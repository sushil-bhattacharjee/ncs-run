# -*- mode: python; python-indent: 4 -*-
import collections
import threading
import ncs.maagic as maagic
from phased_provisioning.constants import Constants
from phased_provisioning.maapi_util import MaapiUtil
from phased_provisioning.ha_util import is_ha, is_ha_switchover_happened


class WorkItem:
    """
    WorkItem class to store details ofeach work item. It has methods to update its phase, such as
    add_pending_item,remove_pending_item, move_item_to_in_progress, and move_to_completed.
    The operational data of each work item is stored in the CDB (Configuration DataBase).
    The WorkItemDeque class implements a thread-safe queue of work items, with methods to enqueue
    and dequeue work items.
    The queue uses a lock and a condition variable to synchronize access to the queue.
    """

    def __init__(self, task_name: str, node_path: str):
        self.__task_name = task_name
        self.__node_path = node_path

    @property
    def node(self):
        return self.__node_path

    @node.setter
    def node(self, path):
        self.__node_path = path

    @property
    def task_name(self):
        return self.__task_name

    @task_name.setter
    def task_name(self, name):
        self.__task_name = name

    def __str__(self):
        return (f'WorkItem:<Task name: {self.task_name}, Node path: {self.node}>')

    # functions to update the phase in the operational data in cdb and phase in WorkItem object
    def set_pending(self, mu: MaapiUtil):
        with mu.trans_write_oper() as th:
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.task_name))
            # Incase of NCS restart incase a WI is moved to pending,clearing old status
            if self.node in task_status.in_progress_nodes:
                del task_status.in_progress_nodes[self.node]
            if self.node in task_status.failed_nodes:
                del task_status.failed_nodes[self.node]
            task_status.pending_nodes.create(self.node)
            th.apply()

    def mark_completed(self, mu: MaapiUtil, elapsed_time: float):
        with mu.trans_write_oper() as th:
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.task_name))
            del task_status.in_progress_nodes[self.node]
            task_status.completed_nodes.create(self.node)
            avg_exec_time = float(task_status.avg_node_exec_time)
            if avg_exec_time > 0.0:
                elapsed_time = (elapsed_time + avg_exec_time) / 2
            task_status.avg_node_exec_time = ('%.6f' % elapsed_time)
            th.apply()

    def mark_failed(self, mu: MaapiUtil, failure_reason: str, reduce_eb: bool = True):
        with mu.trans_write_oper() as th:
            task_status = maagic.get_node(th, Constants.STATUS_PATH.format(self.task_name))
            del task_status.in_progress_nodes[self.node]
            failed_nodes = task_status.failed_nodes
            failed_node = failed_nodes.create(self.node)
            failed_node.failure_reason = failure_reason
            current_eb = task_status.current_error_budget
            if reduce_eb:
                # read from oper because error-budget policy change may happen
                if current_eb != "ignore-failures":
                    current_eb = task_status.current_error_budget - 1
                    task_status.current_error_budget = current_eb
            th.apply()
            return current_eb

    @staticmethod
    def put_all_to_pending(mu: MaapiUtil, work_items: list):
        with mu.trans_write_oper() as th:
            work_item: WorkItem
            for work_item in work_items:
                task_status = maagic.get_node(th, Constants.STATUS_PATH.format(work_item.task_name))
                if work_item.node in task_status.in_progress_nodes:
                    del task_status.in_progress_nodes[work_item.node]
                if work_item.node in task_status.failed_nodes:
                    del task_status.failed_nodes[work_item.node]
                task_status.pending_nodes.create(work_item.node)
            th.apply()

    @staticmethod
    def put_all_to_in_progress(mu: MaapiUtil, work_items: list):
        with mu.trans_write_oper() as th:
            work_item: WorkItem
            for work_item in work_items:
                task_status = maagic.get_node(th, Constants.STATUS_PATH.format(work_item.task_name))
                del task_status.pending_nodes[work_item.node]
                task_status.in_progress_nodes.create(work_item.node)
            th.apply()


class WorkItemDeque:
    def __init__(self):
        self._deque = collections.deque()
        self._q_items = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._process = None
        self._policy_updated = False

    @property
    def process(self):
        return self._process

    @process.setter
    def process(self, p):
        self._process = p

    @property
    def policy_updated(self):
        return self._policy_updated

    @policy_updated.setter
    def policy_updated(self, is_updated):
        self._policy_updated = is_updated

    def enqueue(self, work_item: WorkItem, mu: MaapiUtil):
        with self._condition:
            if work_item.node not in self._q_items:
                work_item.set_pending(mu)
                self._deque.append(work_item)
                self._q_items.append(work_item.node)
                self._condition.notify()

    def enqueue_first(self, work_item: WorkItem, mu: MaapiUtil):
        with self._condition:
            if work_item.node not in self._q_items:
                work_item.set_pending(mu)
                self._deque.appendleft(work_item)
                self._q_items.append(work_item.node)
                self._condition.notify()

    def enqueue_all(self, work_items: list, mu: MaapiUtil):
        with self._condition:
            WorkItem.put_all_to_pending(mu, work_items)
            work_item: WorkItem
            for work_item in work_items:
                if work_item.node not in self._q_items:
                    self._deque.append(work_item)
                    self._q_items.append(work_item.node)
            self._condition.notify()

    def dequeue_batch(self, batch: int, mu: MaapiUtil):
        with self._condition:
            work_items = []
            for _ in range(batch):
                if not self.empty():
                    work_item: WorkItem = self._deque.popleft()
                    self._q_items.remove(work_item.node)
                    work_items.append(work_item)
            WorkItem.put_all_to_in_progress(mu, work_items)
            self._condition.notify()
            return work_items

    def empty(self):
        return not bool(self._deque)

    def aborted(self):
        return TaskMap.getInstance().is_aborted()

    def __len__(self):
        return len(self._deque)

    def __str__(self):
        return f"queue: {self._q_items}"


class TaskMap:
    """
    This is a global map that holds all the pending queues related to phased tasks.
    """
    __instance = None

    @staticmethod
    def getInstance():
        if not TaskMap.__instance:
            TaskMap.__instance = TaskMap()
        return TaskMap.__instance

    def __init__(self):
        if TaskMap.__instance:
            raise Exception("This class is a singleton!")
        else:
            TaskMap.__instance = self
            self.map = {}
            self.lock = threading.Lock()
            self.aborted = False

    def key_exists_in_map(self, key: str) -> bool:
        return key in self.map

    def put(self, key, value: WorkItemDeque):
        with self.lock:
            self.map[key] = value

    def get(self, key: str) -> WorkItemDeque:
        with self.lock:
            return self.map.get(key)

    def abort(self):
        with self.lock:
            self.aborted = True

    def is_aborted(self) -> bool:
        with self.lock:
            return self.aborted

    def delete(self, key):
        with self.lock:
            if self.key_exists_in_map(key):
                del self.map[key]

    def is_empty(self) -> bool:
        with self.lock:
            return not bool(self.map)

    def __str__(self):
        if self.is_empty():
            return "Task Map is empty."
        output = "Task Map:\n"
        for key, value in self.map.items():
            output += f"{key}: {value.__str__()}\n"
        return output

    def recreate(self, mu: MaapiUtil):
        with mu.trans_read_oper() as th:
            root = maagic.get_root(th)
            oper_path = root.phased_provisioning.task_status
            for task_status_node in oper_path:
                if task_status_node.state != 'completed':
                    # Code to be executed if Task state is NOT completed
                    self._restore_task_map(task_status_node, mu)

    def _restore_task_map(self, task_status_node, mu):
        queue = WorkItemDeque()
        self.put(str(task_status_node.name), queue)
        # Enqueue "failed" nodes type-"application communication failure"
        failed_nodes = [WorkItem(str(task_status_node.name), str(node.name))
                        for node in task_status_node.failed_nodes
                        if node.failure_reason
                        and (Constants.NSO_CRASH_ERR_MSG in node.failure_reason
                             or Constants.HA_MODE_ERR_MSG in node.failure_reason)]
        if len(failed_nodes) > 0:
            queue.enqueue_all(failed_nodes, mu)
        # Enqueue "in_progress" nodes in standalone node
        # Enqueue "in_progress" nodes if switch over happened in HA setup.
        if (is_ha() and is_ha_switchover_happened()) or not is_ha():
            in_progress_nodes = [WorkItem(str(task_status_node.name), str(node.name))
                                 for node in task_status_node.in_progress_nodes]
            if len(in_progress_nodes) > 0:
                queue.enqueue_all(in_progress_nodes, mu)
        # Enqueue "pending" nodes
        pending_nodes = [WorkItem(str(task_status_node.name), str(node.name))
                         for node in task_status_node.pending_nodes]
        if len(pending_nodes) > 0:
            queue.enqueue_all(pending_nodes, mu)
