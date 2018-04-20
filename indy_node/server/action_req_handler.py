import dateutil.parser

from plenum.common.exceptions import InvalidClientRequest, \
    UnauthorizedClientRequest
from plenum.common.messages.node_messages import Reply
from plenum.common.txn_util import reqToTxn
from plenum.common.types import f
from plenum.server.req_handler import RequestHandler
from plenum.common.constants import TXN_TYPE, DATA
from indy_common.auth import Authoriser
from indy_common.constants import ACTION, POOL_RESTART, DATETIME, VALIDATOR_INFO
from indy_common.roles import Roles
from indy_common.types import Request
from indy_node.persistence.idr_cache import IdrCache
from indy_node.server.restarter import Restarter
from indy_node.server.pool_config import PoolConfig
from plenum.server.validator_info_tool import ValidatorNodeInfoTool
from stp_core.common.log import getlogger


logger = getlogger()


class ActionReqHandler(RequestHandler):
    operation_types = {POOL_RESTART, VALIDATOR_INFO}

    def __init__(self, idrCache: IdrCache,
                 restarter: Restarter, poolManager, poolCfg: PoolConfig,
                 info_tool: ValidatorNodeInfoTool):
        self.idrCache = idrCache
        self.restarter = restarter
        self.info_tool = info_tool
        self.poolManager = poolManager
        self.poolCfg = poolCfg

    def doStaticValidation(self, request: Request):
        identifier, req_id, operation = request.identifier, request.reqId, request.operation
        if operation[TXN_TYPE] == POOL_RESTART:
            self._doStaticValidationPoolRestart(identifier, req_id, operation)

    def _doStaticValidationPoolRestart(self, identifier, req_id, operation):
        if DATETIME in operation.keys() is None and operation[DATETIME] != "0":
            try:
                dateutil.parser.parse(operation[DATETIME])
            except Exception:
                raise InvalidClientRequest(identifier, req_id,
                                           "time is not valid")

    def validate(self, req: Request):
        status = None
        operation = req.operation
        typ = operation.get(TXN_TYPE)
        if typ not in [POOL_RESTART]:
            return
        origin = req.identifier
        try:
            origin_role = self.idrCache.getRole(origin, isCommitted=False)
        except BaseException:
            raise UnauthorizedClientRequest(
                req.identifier,
                req.reqId,
                "Nym {} not added to the ledger yet".format(origin))
        action = ""
        if typ == POOL_RESTART:
            action = operation.get(ACTION)
        r, msg = Authoriser.authorised(
            typ, origin_role, field=ACTION, oldVal=status, newVal=action)
        if not r:
            raise UnauthorizedClientRequest(
                req.identifier, req.reqId, "{} cannot do restart".format(
                    Roles.nameFromValue(origin_role)))

    def apply(self, req: Request, cons_time: int = None):
        result = {}
        try:
            if req.txn_type == POOL_RESTART:
                self.restarter.handleActionTxn(req)
                result = self._generate_action_result(req)
            elif req.txn_type == VALIDATOR_INFO:
                result = self._generate_action_result(req)
                result[DATA] = self.info_tool.info
            else:
                raise InvalidClientRequest(
                    "{} is not type of action transaction"
                    .format(req.txn_type))
        except Exception as ex:
            if isinstance(ex, InvalidClientRequest):
                raise ex
            result = self._generate_action_result(req,
                                                  False,
                                                  ex.args[0])
            logger.warning("Operation is failed")
        finally:
            return result

    def _generate_action_result(self, request: Request, is_success=True,
                                msg=None):
        return {**request.operation, **{
            f.IDENTIFIER.nm: request.identifier,
            f.REQ_ID.nm: request.reqId,
            f.IS_SUCCESS.nm: is_success,
            f.MSG.nm: msg}}
