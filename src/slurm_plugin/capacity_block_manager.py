# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List

from common.schedulers.slurm_reservation_commands import (
    create_slurm_reservation,
    delete_slurm_reservation,
    does_slurm_reservation_exist,
    get_slurm_reservations_info,
    update_slurm_reservation,
)
from common.time_utils import seconds_to_minutes
from slurm_plugin.slurm_resources import SlurmNode

from aws.ec2 import CapacityBlockReservationInfo, Ec2Client

logger = logging.getLogger(__name__)

# Time in minutes to wait to retrieve Capacity Block Reservation information from EC2
CAPACITY_BLOCK_RESERVATION_UPDATE_PERIOD = 10
SLURM_RESERVATION_NAME_PREFIX = "pcluster-"


class CapacityType(Enum):
    """Enum to identify the type compute supported by the queues."""

    CAPACITY_BLOCK = "capacity-block"
    ONDEMAND = "on-demand"
    SPOT = "spot"


class CapacityBlock:
    """
    Class to store Capacity Block info from EC2 and fleet config.

    Contains info like:
    - queue and compute resource from the config,
    - state from EC2,
    - name of the related slurm reservation.
    """

    def __init__(self, capacity_block_id, queue_name, compute_resource_name):
        self.capacity_block_id = capacity_block_id
        self.queue_name = queue_name
        self.compute_resource_name = compute_resource_name
        self._capacity_block_reservation_info = None
        self._nodenames = []

    def update_ec2_info(self, capacity_block_reservation_info: CapacityBlockReservationInfo):
        """Update info from CapacityBlockReservationInfo."""
        self._capacity_block_reservation_info = capacity_block_reservation_info

    def slurm_reservation_name(self):
        """Retrieve slurm reservation associated."""
        return f"{SLURM_RESERVATION_NAME_PREFIX}{self.capacity_block_id}"

    def add_nodename(self, nodename: str):
        """Add node name to the list of nodenames associated to the capacity block."""
        self._nodenames.append(nodename)

    def nodenames(self):
        """Return list of nodenames associated with the capacity block."""
        return self._nodenames

    def state(self):
        """Return state of the CB: payment-pending, pending, active, expired, and payment-failed."""
        return self._capacity_block_reservation_info.state()

    def is_active(self):
        """Return true if CB is in active state."""
        return self.state() == "active"

    def does_node_belong_to(self, node):
        """Return true if the node belongs to the CB."""
        return node.queue_name == self.queue_name and node.compute_resource_name == self.compute_resource_name

    @staticmethod
    def slurm_reservation_name_to_id(slurm_reservation_name: str):
        """Parse slurm reservation name to retrieve related capacity block id."""
        return slurm_reservation_name[len(SLURM_RESERVATION_NAME_PREFIX) :]  # noqa E203

    @staticmethod
    def is_capacity_block_slurm_reservation(slurm_reservation_name: str):
        """Return true if slurm reservation name is related to a CB, if it matches a specific internal convention."""
        return slurm_reservation_name.startswith(SLURM_RESERVATION_NAME_PREFIX)

    def __eq__(self, other):
        return (
            self.capacity_block_id == other.capacity_block_id
            and self.queue_name == other.queue_name
            and self.compute_resource_name == other.compute_resource_name
        )


class CapacityBlockManager:
    """Capacity Block Reservation Manager."""

    def __init__(self, region, fleet_config, boto3_config):
        self._region = region
        self._fleet_config = fleet_config
        self._boto3_config = boto3_config
        self._ec2_client = None
        # internal variables to store Capacity Block info from fleet config and EC2
        self._capacity_blocks: Dict[str, CapacityBlock] = {}
        self._capacity_blocks_update_time = None
        self._reserved_nodenames: List[str] = []

    @property
    def ec2_client(self):
        if not self._ec2_client:
            self._ec2_client = Ec2Client(config=self._boto3_config)
        return self._ec2_client

    def get_reserved_nodenames(self, nodes: List[SlurmNode]):
        """Manage nodes part of capacity block reservation. Returns list of reserved nodes."""
        # evaluate if it's the moment to update info
        is_time_to_update = (
            self._capacity_blocks_update_time
            and seconds_to_minutes(datetime.now(tz=timezone.utc) - self._capacity_blocks_update_time)
            > CAPACITY_BLOCK_RESERVATION_UPDATE_PERIOD
        )
        if is_time_to_update:  # TODO: evaluate time to update accordingly to capacity block start time
            reserved_nodenames = []

            # update capacity blocks details from ec2 (e.g. state)
            self._update_capacity_blocks_info_from_ec2()
            # associate nodenames to capacity blocks, according to queues and compute resources from fleet configuration
            self._associate_nodenames_to_capacity_blocks(nodes)

            # create, update or delete slurm reservation for the nodes according to CB details.
            for capacity_block in self._capacity_blocks.values():
                reserved_nodenames.extend(self._update_slurm_reservation(capacity_block))
            self._reserved_nodenames = reserved_nodenames

            # delete slurm reservations created by CapacityBlockManager not associated to existing capacity blocks
            self._cleanup_leftover_slurm_reservations()

        return self._reserved_nodenames

    def _associate_nodenames_to_capacity_blocks(self, nodes: List[SlurmNode]):
        """
        Update capacity_block info adding nodenames list.

        Check configured CBs and associate nodes to them according to queue and compute resource info.
        """
        for node in nodes:
            capacity_block: CapacityBlock
            for capacity_block in self._capacity_blocks.values():
                if capacity_block.does_node_belong_to(node):
                    capacity_block.add_nodename(node.name)
                    break

    def _cleanup_leftover_slurm_reservations(self):
        """Find list of slurm reservations created by ParallelCluster but not part of the configured CBs."""
        slurm_reservations = get_slurm_reservations_info()
        for slurm_reservation in slurm_reservations:
            if CapacityBlock.is_capacity_block_slurm_reservation(slurm_reservation.name):
                capacity_block_id = CapacityBlock.slurm_reservation_name_to_id(slurm_reservation.name)
                if capacity_block_id not in self._capacity_blocks.keys():
                    logger.info(
                        (
                            "Found leftover slurm reservation %s for nodes %s. "
                            "Related Capacity Block %s is no longer in the cluster configuration. "
                            "Deleting the slurm reservation."
                        ),
                        slurm_reservation.name,
                        slurm_reservation.nodes,
                        capacity_block_id,
                    )
                    delete_slurm_reservation(name=slurm_reservation.name)
            else:
                logger.debug(
                    "Slurm reservation %s is not managed by ParallelCluster. Skipping it.", slurm_reservation.name
                )

    @staticmethod
    def _update_slurm_reservation(capacity_block: CapacityBlock):
        """
        Update Slurm reservation associated to the given Capacity Block.

        A CB has five possible states: payment-pending, pending, active, expired and payment-failed,
        we need to create/delete Slurm reservation accordingly.

        returns list of nodes reserved for that capacity block, if it's not active.
        """

        def _log_cb_info(action_info):
            logger.info(
                "Capacity Block reservation %s is in state %s. %s Slurm reservation %s for nodes %s.",
                capacity_block.capacity_block_id,
                capacity_block.state(),
                action_info,
                slurm_reservation_name,
                capacity_block_nodenames,
            )

        nodes_in_slurm_reservation = []

        # retrieve list of nodes associated to a given slurm reservation/capacity block
        slurm_reservation_name = capacity_block.slurm_reservation_name()
        capacity_block_nodes = capacity_block.nodenames()
        capacity_block_nodenames = ",".join(capacity_block_nodes)

        reservation_exists = does_slurm_reservation_exist(name=slurm_reservation_name)
        # if CB is active we need to remove Slurm reservation and start nodes
        if capacity_block.is_active():
            # if Slurm reservation exists, delete it.
            if reservation_exists:
                _log_cb_info("Deleting related")
                delete_slurm_reservation(name=slurm_reservation_name)
            else:
                _log_cb_info("Nothing to do. No existing")

        # if CB is expired or not active we need to (re)create Slurm reservation
        # to avoid considering nodes as unhealthy
        else:
            nodes_in_slurm_reservation = capacity_block_nodes
            # create or update Slurm reservation
            if reservation_exists:
                _log_cb_info("Updating existing related")
                update_slurm_reservation(name=slurm_reservation_name, nodes=capacity_block_nodenames)
            else:
                _log_cb_info("Creating related")
                create_slurm_reservation(
                    name=slurm_reservation_name,
                    start_time=datetime.now(tz=timezone.utc),
                    nodes=capacity_block_nodenames,
                )

        return nodes_in_slurm_reservation

    def _update_capacity_blocks_info_from_ec2(self):
        """
        Store in the _capacity_reservations a dict for CapacityReservation info.

        This method is called every time the CapacityBlockManager is re-initialized,
        so when it starts/is restarted or when fleet configuration changes.
        """
        # Retrieve updated capacity reservation information at initialization time, and every tot minutes
        self._capacity_blocks = self._capacity_blocks_from_config()

        if self._capacity_blocks:
            capacity_block_ids = self._capacity_blocks.keys()
            logger.info(
                "Retrieving updated Capacity Block reservation information from EC2 for %s",
                ",".join(capacity_block_ids),
            )
            capacity_block_reservations_info: List[
                CapacityBlockReservationInfo
            ] = self.ec2_client().describe_capacity_reservations(capacity_block_ids)

            for capacity_block_reservation_info in capacity_block_reservations_info:
                capacity_block_id = capacity_block_reservation_info.capacity_reservation_id()
                self._capacity_blocks[capacity_block_id].update_ec2_info(capacity_block_reservation_info)

        self._capacity_blocks_update_time = datetime.now(tz=timezone.utc)

    def _capacity_blocks_from_config(self):
        """
        Collect list of capacity reservation target from all queues/compute-resources in the fleet config.

        Fleet config json has the following format:
        {
            "my-queue": {
                "my-compute-resource": {
                   "Api": "create-fleet",
                    "CapacityType": "on-demand|spot|capacity-block",
                    "AllocationStrategy": "lowest-price|capacity-optimized|use-capacity-reservations-first",
                    "Instances": [
                        { "InstanceType": "p4d.24xlarge" }
                    ],
                    "MaxPrice": "",
                    "Networking": {
                        "SubnetIds": ["subnet-123456"]
                    },
                    "CapacityReservationId": "id"
                }
            }
        }
        """
        capacity_blocks: Dict[str, CapacityBlock] = {}
        logger.info("Retrieving Capacity Block reservation information for fleet config.")

        for queue_name, queue_config in self._fleet_config.items():
            for compute_resource_name, compute_resource_config in queue_config.items():
                if self._is_compute_resource_associated_to_capacity_block(compute_resource_config):
                    capacity_block_id = self._capacity_reservation_id_from_compute_resource_config(
                        compute_resource_config
                    )
                    capacity_block = CapacityBlock(
                        capacity_block_id=capacity_block_id,
                        queue_name=queue_name,
                        compute_resource_name=compute_resource_name,
                    )
                    capacity_blocks.update({capacity_block_id: capacity_block})

        return capacity_blocks

    @staticmethod
    def _is_compute_resource_associated_to_capacity_block(compute_resource_config):
        """Return True if compute resource is associated to a Capacity Block reservation."""
        capacity_type = compute_resource_config.get("CapacityType", CapacityType.ONDEMAND)
        return capacity_type == CapacityType.CAPACITY_BLOCK.value

    @staticmethod
    def _capacity_reservation_id_from_compute_resource_config(compute_resource_config):
        """Return capacity reservation target if present, None otherwise."""
        try:
            return compute_resource_config["CapacityReservationId"]
        except KeyError as e:
            # This should never happen because this file is created by cookbook config parser
            logger.error(
                "Unable to retrieve CapacityReservationId from compute resource info: %s", compute_resource_config
            )
            raise e
