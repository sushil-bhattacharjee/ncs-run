# -*- mode: python; python-indent: 4 -*-
"""Module for processing phased provisioning work items."""
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any
from string import Template
from ncs import maagic, maapi
from phased_provisioning.namespaces.ciscoPhasedProvisioning_ns import ns as pdp_ns
from phased_provisioning.pdp_task_map import WorkItem, WorkItemDeque
from phased_provisioning.constants import Constants
from phased_provisioning.util import get_current_time
from phased_provisioning.ha_util import validate_ha_mode
from phased_provisioning.maapi_util import MaapiUtil
from phased_provisioning.pdp_task_data import (PDPException, PDPActionException, PDPTestException,
                                               TaskData, TaskProcessData)


class WorkItemProcess:
    """Phased provisioning work item processor that encapsulates the capability of executing
    main action and test action/expression.
    """
    def __init__(self, task_data: TaskData, log) -> None:
        self.task_data = task_data
        self.log = log

    def _get_action(self, context: str, action: str) -> str:
        """get absolute action path or relative path
            Context: Target node path as string
            Action: Action to be executed as string
        """
        # xpath is absolute if starts with "/"
        if action.startswith("/"):
            action = action
        else:
            # xpath is relative path in context if doesn't start with "/"
            action = context + "/" + str(action)
        return action

    def __call__(self, work_item: WorkItem) -> Any:
        """Process a work item, understands and executes the main action and test action/expression
            work_item: a WorkItem object containing the target node data
        """
        node_path = f'{work_item.node}'
        self.log.info(f"work item: {node_path}")
        try:
            with self.task_data.mu.trans_read_running() as th:
                if not th.exists(node_path):
                    raise PDPException(f'Path does not exist : {node_path}')

                start = get_current_time(False)
                self._execute_action(th, node_path)
                self._execute_test(th, node_path)
                elapsed_time = (get_current_time(False) - start).total_seconds()
                self.log.debug(f"{node_path} execution time: {elapsed_time} seconds.")
                with self.task_data.lock:
                    work_item.mark_completed(self.task_data.mu, elapsed_time)
        except PDPException as pdp_exp:
            self._handle_exception(work_item, pdp_exp)
        except Exception as exp:
            self.log.exception(f"Error processing node {node_path}: ", exp)
            self._handle_exception(work_item, exp, False)

    def _handle_exception(self, work_item: WorkItem, exp: Exception, is_reduce_eb=True):
        if Constants.NSO_CRASH_ERR_MSG in str(exp):
            is_reduce_eb = False
        # mark node fail and reduce error budget in a single transaction
        with self.task_data.lock:
            self.task_data.error_budget_left = work_item.mark_failed(
                self.task_data.mu, str(exp), is_reduce_eb)

    def _execute_action(self, th: maapi.Transaction, context: str) -> None:
        """Handles and overlooks the main action execution
            Th: Transaction
            Context: Target node path as string
        """
        stage = Constants.ACTION
        action = self.task_data.action_name
        log_prefix = f"{context} - {stage}:"
        try:
            args = self._eval_variables(th, context, self.task_data.variable)
            outparams = self._call_action(th, context, action, args, log_prefix)
            self._eval_action_output(outparams, action, stage, log_prefix)
        except Exception as exp:
            self.log.exception(f"{log_prefix} Error executing action {action}: ", exp)
            if Constants.HA_MODE_ERR_MSG in str(exp):
                raise Exception(f"{stage}: {exp}")
            raise PDPActionException(f"{stage}: {exp}")

    def _execute_test(self, th: maapi.Transaction, context: str) -> None:
        """Handles and overlooks test on the work item
            Th: Transaction
            Context: Target node path as string
        """
        stage = Constants.SELF_TEST
        log_prefix = f"{context} - {stage}:"
        st_type = self.task_data.test_type
        try:
            if st_type == pdp_ns.cisco_pdp_action_name_:
                action = self.task_data.self_test_action
                st_type = f"action: {action}"
                args = self._eval_variables(th, context, self.task_data.self_test_variable)
                outparams = self._call_action(th, context, action, args, log_prefix)
                self._eval_action_output(outparams, action, stage, log_prefix)
            elif st_type == pdp_ns.cisco_pdp_test_expr_:
                st_type += f": {self.task_data.test_expr}"
                args = self._eval_variables(th, context, self.task_data.self_test_variable)
                subs_test_expr = self._expr_subsitute(self.task_data.test_expr, args)
                self.log.debug(f"{log_prefix} test expression after subsitute is {subs_test_expr}")
                expr_result = self._eval_xpath(th, context, subs_test_expr)
                if bool(expr_result) is False:
                    raise Exception(f"Evaluation of test-expr result is: {expr_result}")
            else:
                self.log.info(f"{log_prefix}: No test configured.")
        except Exception as exp:
            self.log.exception(f"{log_prefix} Error executing {st_type} - {exp}")
            if Constants.HA_MODE_ERR_MSG in str(exp):
                raise Exception(f"{stage}: {exp}")
            raise PDPTestException(f"{stage}: {exp}")

    def _eval_xpath(self, th: maapi.Transaction, context: str, xpath) -> str:
        """Evaluate Xpath on context path
        """
        return th.xpath_eval_expr(xpath, None, context)

    def _eval_variables(self, th: maapi.Transaction, context: str, variable: Any) -> dict:
        """Evaluate variables for action and self-test action
        """
        args = {}
        if isinstance(variable, dict):
            args = self._eval_dict_variables(th, context, variable)
        else:
            # TODO: Handle xml payload for actions
            pass
        return args

    def _eval_dict_variables(self, th: maapi.Transaction, context: str, args: dict) -> dict:
        """Evaluate variables and resolve xpath value from a dictionary.
        """
        res_args = {}
        for key, value in args.items():
            if value:
                if value[0] == Constants.VAR_EXPR:
                    expr_xpath = value[1]
                    val = self._eval_xpath(th, context, expr_xpath)
                else:
                    val = value[1]
            else:
                val = value
            res_args[key] = val
        return res_args

    def _expr_subsitute(self, test_expr, args: dict) -> str:
        """After evaluate the result of task variables, replace them into self-test
            test-expr and generate a string
            Test_expr: test-expr configured by user
            Args: Dictionary saved the var,value/expr pairs for us to replace into test-expr
        """
        res_s = Template(str(test_expr))
        return res_s.substitute(**args)

    def _call_action(self, th: maapi.Transaction, context: str, action: str, args: Any,
                     log_prefix: str) -> maagic.ActionParams:
        """Executes action
            Th: Transaction
            Context: Target node path as string
            Action: The action to be invoked as string
            Args: Map of key value pairs or a String in case of xml payload
        """
        self.log.info(f'{log_prefix} Executing action: {action}')
        action = self._get_action(context, action)
        action_node = maagic.get_node(th, action)
        action_input = action_node.get_input()

        if args:
            for key, value in args.items():
                if value:
                    action_input[key] = value
                else:
                    action_input[key].create()

        validate_ha_mode()
        return action_node(action_input)

    def _eval_action_output(self, outparams: maagic.ActionParams, action: str,
                            stage: str, log_prefix: str):
        if hasattr(outparams, "result"):
            result = outparams.result
            if type(result) is not bool and type(result) is not int:
                raise Exception(f"The action {action} output result type"
                                " is not an integer or boolean.")
            elif ((type(result) is bool and bool(result) is not True)
                  or (type(result) is int and int(result) != 0)):
                err_leafs = ["info", "detail", "error"]
                exp_str = ""
                for leaf in err_leafs:
                    if hasattr(outparams, leaf) and outparams[leaf]:
                        exp_str += str(outparams[leaf])
                        break
                if not exp_str:
                    self.log.warning(
                        f"{log_prefix} The action {action} output error message could not be "
                        f"determined, because any one of the {'/'.join(err_leafs)} is not present."
                        " Raising exception with standard failure message.")
                    exp_str += f"The action {action} has failed."
                raise Exception(exp_str)
            else:
                self.log.debug(f"{log_prefix} The action {action} has passed,"
                               f" action output result is {result}")
        else:
            if Constants.SELF_TEST == stage:
                raise Exception(f"The action {action} output does not have result.")
            self.log.warning(f"{log_prefix} The action {action} output could not be validated,"
                             " because result is not present!!!")


def batch_process(tp_data: TaskProcessData, queue: WorkItemDeque, log, mu: MaapiUtil):
    """Batch processor that consumes the queue items and schedules the requests in batches.
       It can track the window and error budget and schedules batches accordingly.
    """
    try:
        queue.process = tp_data
        log.info(f"pending queue size: {len(queue)}")
        while (not queue.aborted() and not queue.empty()):
            batch_size = tp_data.task_data.batch
            if tp_data.is_break_batch_process():
                break

            # Get the next batch of work items from the queue.
            batch = queue.dequeue_batch(batch_size, mu)
            wi_size = len(batch)
            log.info(f"work items size: {wi_size} ")
            # start the batch process
            with ThreadPoolExecutor(max_workers=wi_size,
                                    thread_name_prefix=tp_data.task_data.name) as executor:
                fs = [executor.submit(
                    WorkItemProcess(tp_data.task_data, log), arg) for arg in batch]
                wait(fs)
                log.info(f"One batch of {wi_size} work items completed.")

            # check if policy has been updated
            if queue.policy_updated and not queue.empty():
                stop_next_batch, is_reset_window = tp_data.task_data.process_policy_updates()
                queue.policy_updated = False
                if stop_next_batch:
                    tp_data.task_data.update_task_status(
                        pdp_ns.cisco_pdp_pdp_in_progress_,
                        "batch process stopped, because policy scheduler time changed.")
                    break
                if is_reset_window:
                    tp_data.reset_window()

        if queue.aborted():
            log.info("**************************ABORTED**************************")
        elif queue.empty() and not tp_data.task_data.is_task_suspended():
            tp_data.task_data.set_status_completed()
            queue.policy_updated = False
    except Exception as exception:
        log.exception(f"Batch process error: {exception}")
    finally:
        # if thread is detached while there is another attempt to use
        # the same thread in the meaintime, the thread will die doing nothing.
        queue.process = None
