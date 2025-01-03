# -*- mode: python; python-indent: 4 -*-
import logging
from contextlib import contextmanager
import ncs
from ncs import maapi
from phased_provisioning.constants import Constants


class MaapiUtil:
    def __init__(self):
        self.__load_user()

    @contextmanager
    def trans_write_oper(self):
        with maapi.single_write_trans(self.l_user, Constants.MAAPI_CTX, db=ncs.OPERATIONAL,
                                      vendor=Constants.MAAPI_VENDOR,
                                      product=Constants.MAAPI_PRODUCT) as th:
            yield th

    @contextmanager
    def trans_read_oper(self):
        with maapi.single_read_trans(self.l_user, Constants.MAAPI_CTX, db=ncs.OPERATIONAL,
                                     vendor=Constants.MAAPI_VENDOR,
                                     product=Constants.MAAPI_PRODUCT) as th:
            yield th

    @contextmanager
    def trans_write_running(self):
        with maapi.single_write_trans(self.l_user, Constants.MAAPI_CTX,
                                      vendor=Constants.MAAPI_VENDOR,
                                      product=Constants.MAAPI_PRODUCT) as th:
            yield th

    @contextmanager
    def trans_read_running(self):
        with maapi.single_read_trans(self.l_user, Constants.MAAPI_CTX,
                                     vendor=Constants.MAAPI_VENDOR,
                                     product=Constants.MAAPI_PRODUCT) as th:
            yield th

    def __load_user(self):
        '''
            Reads local-user from cisco-phased-provisioning.yang.
            local-user should be used for read/write maapi sessions.
        '''
        self.l_user = ""
        try:
            with self.trans_read_running() as th:
                self.l_user = str(ncs.maagic.get_node(th, Constants.LOCAL_USR_PATH))
        except Exception as exp:
            logging.getLogger(Constants.MAAPI_PRODUCT).warning(
                f"Could not read phased-provisioning/local-user: {exp}")
