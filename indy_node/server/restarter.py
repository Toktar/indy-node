import os
from collections import deque
from datetime import datetime, timedelta
from functools import partial
from typing import Tuple, Union, Optional, Callable, Dict

import dateutil.parser
import dateutil.tz

from indy_node.server.node_maintainer import NodeMaintainer
from indy_node.server.restart_log import RestartLog
from stp_core.common.log import getlogger
from plenum.common.constants import TXN_TYPE, VERSION, DATA, IDENTIFIER
from plenum.common.types import f
from plenum.server.has_action_queue import HasActionQueue
from indy_common.constants import ACTION, POOL_RESTART, START, SCHEDULE, \
    CANCEL, JUSTIFICATION, TIMEOUT, REINSTALL, IN_PROGRESS, FORCE
from plenum.server import notifier_plugin_manager
from ledger.util import F
import asyncio

logger = getlogger()


class Restarter(NodeMaintainer):

    def _defaultLog(self, dataDir, config):
        log = os.path.join(dataDir, config.restartLogFile)
        return RestartLog(filePath=log)

    def _is_action_started(self):
        if not self.lastActionEventInfo:
            logger.debug('Node {} has no restart events'
                         .format(self.nodeName))
            return False

        (event_type, when) = self.lastActionEventInfo

        if event_type != RestartLog.RESTART_STARTED:
            logger.debug(
                'Restart for node {} was not scheduled. Last event is {}:{}:{}'.format(
                    self.nodeName, event_type, when))
            return False

        return True

    def _update_action_log_for_started_action(self):
        (event_type, when) = self.lastActionEventInfo

        if not self.didLastExecutedRestartSucceeded:
            self._actionLog.appendFailed(when)
            self._action_failed(scheduled_on=when,
                                external_reason=True)
            return

        self._actionLog.appendSucceeded(when)
        logger.info("Node '{}' successfully restarted"
                    .format(self.nodeName))
        self._notifier.sendMessageUponNodeRestartComplete(
            "Restart of node '{}' scheduled on {} "
            "completed successfully"
                .format(self.nodeName, when))

    def handleActionTxn(self, txn) -> None:
        """
        Handles transaction of type POOL_RESTART
        Can schedule or cancel restart to a newer
        version at specified time

        :param txn:
        """
        FINALIZING_EVENT_TYPES = [
            RestartLog.RESTART_SUCCEEDED, RestartLog.RESTART_FAILED]

        if txn[TXN_TYPE] != POOL_RESTART:
            return

        when = txn[SCHEDULE] if SCHEDULE in txn.keys() else None
        if isinstance(when, str) and when != "0":
            when = dateutil.parser.parse(when)
        now = datetime.utcnow().replace(tzinfo=dateutil.tz.tzutc())
        if when is None or when == "0" or now >= when:
            msg = RestartMessage(action=POOL_RESTART).toJson()
            try:
                asyncio.ensure_future(self._open_connection_and_send(msg))
            except Exception as ex:
                logger.warning(ex.args[0])
            return

        action = txn[ACTION]
        justification = txn.get(JUSTIFICATION)

        if action == START:
            # forced txn could have partial schedule list
            if self.nodeId not in txn[SCHEDULE]:
                logger.info("Node '{}' disregards restart txn {}".format(
                    self.nodeName, txn))
                return

            last_event = self.lastActionEventInfo
            if last_event and last_event[
                0] in FINALIZING_EVENT_TYPES:
                logger.info(
                    "Node '{}' has already performed an restart. "
                    "Last recorded event is {}".format(
                        self.nodeName, last_event))
                return

            failTimeout = txn.get(TIMEOUT, self.defaultActionTimeout)

            if self.scheduledAction:
                if isinstance(when, str):
                    when = dateutil.parser.parse(when)
                if self.scheduledAction == when:
                    logger.debug(
                        "Node {} already scheduled restart".format(
                            self.nodeName))
                    return
                else:
                    logger.info(
                        "Node '{}' cancels previous restart and schedules a new one".format(
                            self.nodeName))
                    self._cancelScheduledRestart(justification)

            logger.info("Node '{}' schedules restart".format(
                self.nodeName))

            self._scheduleRestart(when, failTimeout)
            return

        if action == CANCEL:
            if self.scheduledAction:
                self._cancelScheduledRestart(justification)
                logger.info("Node '{}' cancels restart".format(
                    self.nodeName))
            return

        logger.error(
            "Got {} transaction with unsupported action {}".format(
                POOL_RESTART, action))

    def _scheduleRestart(self,
                         when: Union[datetime, str],
                         failTimeout) -> None:
        """
        Schedules node restart to a newer version

        :param version: version to restart to
        :param when: restart time
        """
        assert isinstance(when, (str, datetime))
        logger.info("{}'s restartr processing restart"
                    .format(self))
        if isinstance(when, str):
            when = dateutil.parser.parse(when)
        now = datetime.utcnow().replace(tzinfo=dateutil.tz.tzutc())

        self._notifier.sendMessageUponNodeRestartScheduled(
            "Restart of node '{}' has been scheduled on {}".format(
                self.nodeName, when))
        self._actionLog.appendScheduled(when)

        callAgent = partial(self._callRestartAgent, when,
                            failTimeout)
        delay = 0
        if now < when:
            delay = (when - now).total_seconds()
        self.scheduledAction = (when)
        self._schedule(callAgent, delay)

    def _cancelScheduledRestart(self, justification=None) -> None:
        """
        Cancels scheduled restart

        :param when: time restart was scheduled to
        :param version: version restart scheduled for
        """

        if self.scheduledAction:
            why_prefix = ": "
            why = justification
            if justification is None:
                why_prefix = ", "
                why = "cancellation reason not specified"

            when = self.scheduledAction
            logger.info("Cancelling restart"
                        " of node {node}"
                        " scheduled on {when}"
                        "{why_prefix}{why}"
                        .format(node=self.nodeName,
                                when=when,
                                why_prefix=why_prefix,
                                why=why))

            self._unscheduleAction()
            self._actionLog.appendCancelled(when)
            self._notifier.sendMessageUponPoolRestartCancel(
                "Restart of node '{}'"
                "has been cancelled due to {}".format(
                    self.nodeName, why))

    def _callRestartAgent(self, when, failTimeout) -> None:
        """
        Callback which is called when restart time come.
        Writes restart record to restart log and asks
        node control service to perform restart

        :param when: restart time
        :param version: version to restart to
        """

        logger.info("{}'s restartr calling agent for restart".format(self))
        self._actionLog.appendStarted(when)
        self._action_start_callback()
        self.scheduledAction = None
        asyncio.ensure_future(
            self._sendUpdateRequest(when, failTimeout))

    async def _sendUpdateRequest(self, when, failTimeout):
        retryLimit = self.retry_limit
        while retryLimit:
            try:
                msg = RestartMessage(action=POOL_RESTART).toJson()
                logger.info("Sending message to control tool: {}".format(msg))
                await self._open_connection_and_send(msg)
                break
            except Exception as ex:
                logger.warning("Failed to communicate to control tool: {}"
                               .format(ex))
                asyncio.sleep(self.retry_timeout)
                retryLimit -= 1
        if not retryLimit:
            self._action_failed(scheduled_on=when,
                                reason="problems in communication with "
                                       "node control service")
            self._unscheduleAction()
            self._actionFailedCallback()
        else:
            logger.info("Waiting {} minutes for restart to be performed"
                        .format(failTimeout))
            timesUp = partial(self._declareTimeoutExceeded, when)
            self._schedule(timesUp, self.get_timeout(failTimeout))

    def _declareTimeoutExceeded(self, when):
        """
        This function is called when time for restart is up
        """

        logger.info("Timeout exceeded for {}".format(when))
        last = self._actionLog.lastEvent
        if last and last[1:-1] == (RestartLog.RESTART_FAILED, when):
            return None

        self._action_failed(scheduled_on=when,
                            reason="exceeded restart timeout")

        self._unscheduleAction()
        self._actionFailedCallback()

    def _action_failed(self, *,
                       scheduled_on,
                       reason=None,
                       external_reason=False):
        if reason is None:
            reason = "unknown reason"
        error_message = "Node {node} failed restart" \
                        "scheduled on {scheduled_on} " \
                        "because of {reason}" \
            .format(node=self.nodeName,
                    scheduled_on=scheduled_on,
                    reason=reason)
        logger.error(error_message)
        if external_reason:
            logger.error("This problem may have external reasons, "
                         "check syslog for more information")
        self._notifier.sendMessageUponNodeRestartFail(error_message)


class RestartMessage:
    """
    Data structure that represents request for node update
    """

    def __init__(self, action: str):
        self.action = action

    def toJson(self):
        import json
        return json.dumps(self.__dict__)