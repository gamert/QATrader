import datetime
import json
import threading
import time
import pymongo

import QUANTAXIS as QA
# from QA_OTGBroker import (cancel_order, change_password, login, on_close,
#                           on_error, on_message, peek, querybank, send_order,
#                           subscribe_quote, transfer, websocket, query_settlement)
from QAPUBSUB import consumer, producer
from QATRADER.setting import (
    trade_server_account_exchange, trade_server_ip,
    trade_server_order_exchange, trade_server_password, trade_server_port,
    trade_server_user)
from QATRADER.util import fix_dict
from QUANTAXIS.QAEngine import QA_Thread
from qaenv import mongo_ip, mongo_uri, eventmq_ip, eventmq_port, eventmq_username, eventmq_password, eventmq_amqp

# websocket.enableTrace(True)
"""
1. publisher : ==> QAACCOUNT EXCHANGE
2. consumer  : ==> QAORDER_ROUTER EXCHANGE
"""

##
class QA_TRADER_OKEX(QA_Thread):
    """trade_account 是一个线程/ 具备websocket能力/ 实例化为一个可以交易的websocket型账户

    Arguments:
        QA_Thread {[type]} -- [description]

    Raises:
        Exception -- [description]

    Returns:
        [type] -- [description]
    """

    def __init__(self, api_key, password, secret_key, wsuri, broker_name='simnow', portfolio='default',
                 eventmq_ip=eventmq_ip, eventmq_port=eventmq_port, sig=True, ping_gap=1,
                 bank_password=None, capital_password=None, appid=None,
                 if_restart=True, taskid=None, database_ip = mongo_ip):

        super().__init__(name='QATRADER_{}'.format(api_key))
        self.api_key = api_key
        self.password = password
        self.secret_key = secret_key
        self.broker_name = broker_name
        self.wsuri = wsuri
        self.pub = producer.publisher_routing(
            exchange=trade_server_account_exchange, host=eventmq_ip, port=eventmq_port)
        #self.ws = self.generate_websocket()
        # 使用http交易:
        self.accountAPI = account.AccountAPI(api_key, secret_key, password, False)
        self.spotAPI = spot.SpotAPI(api_key, secret_key, passphrase, False)


        """几个涉及跟数据库交互的client
        """
        database = pymongo.MongoClient(mongo_ip)
        self.account_client = database.QAREALTIME.account
        self.settle_client = database.QAREALTIME.history
        self.xhistory = database.QAREALTIME.hisaccount
        # 保存下单..
        self.orders_client = database.QAREALTIME.orders
        self.portfolio = portfolio
        self.connection = True
        self.message = {'password': password, 'wsuri': wsuri, 'broker_name': broker_name, 'portfolio': portfolio, 'taskid': taskid, 'updatetime': str(
            datetime.datetime.now()), 'accounts': {}, 'orders': {}, 'positions': {}, 'trades': {}, 'banks': {}, 'transfers': {}, 'event': {},  'eventmq_ip': eventmq_ip,
            'ping_gap': ping_gap, 'api_key': api_key, 'bank_password': bank_password, 'capital_password': capital_password, 'settlement': {},
            'bankid': 0, 'money': 0, 'investor_name': ''}
        self.last_update_time = datetime.datetime.now()
        self.sig = sig
        self.if_restart = if_restart
        self.ping_gap = ping_gap
        self.bank_password = bank_password
        self.capital_password = capital_password
        self.tempPass = ''
        self.appid = appid
        #监听交易下单指令: trade_server_order_exchange
        self.sub = consumer.subscriber_routing(host=eventmq_ip, port=eventmq_port, user=trade_server_user, password=trade_server_password,
                                               exchange=trade_server_order_exchange, routing_key=self.api_key)
        #转发交易命令:QATRANSACTION
        self.pub_transaction = producer.publisher_routing(
            host=eventmq_ip, exchange='QATRANSACTION')
        self.sub.callback = self.callback


    def on_close(self):
        QA.QA_util_log_expection('TRADE LOG OUT! CONNECTION LOST')

        self.message['status'] = 500
        self.update_account()
        self.settle()

    def on_message(self,  message):

        message = message if isinstance(
            message, dict) else json.loads(str(message))

        message = fix_dict(message)
        # 单纯转发?
        self.pub.pub(json.dumps(message), routing_key=self.api_key)
        """需要在这里维持实时账户逻辑

        accounts ==> 直接覆盖
        positions ==> 增量
        trade ==> 增量

        """

        if message['aid'] in ["rtn_data", 'qry_settlement_info']:
            self.sync()
            self.handle(message)


    def update_account(self):
        QA.QA_util_log_info('updateAccount')
        self.account_client.update_one({'api_key': self.api_key}, {
            '$set': fix_dict(self.message)}, upsert=True)

    def updateSinglekey(self, singlekey, newdata):
        """更新单个业务字段

        Arguments:
            singlekey {[type]} -- [description]
            newdata {[type]} -- [description]
        """
        try:
            self.message[singlekey] = self.update(
                self.message[singlekey], newdata[singlekey])
        except Exception as e:
            print(e)

    # 处理下单和查询返回:
    def handle(self, message):
        if message['aid'] == "rtn_data":

            try:
                data = message['data'][0]['trade']
                # if 'session' in data

                api_key = str(list(data.keys())[0])
                #user_id = data[api_key]['user_id']
                self.last_update_time = datetime.datetime.now()
                self.message['updatetime'] = str(
                    self.last_update_time)
                new_message = data[api_key]

                if 'session' in new_message.keys():
                    self.trading_day = new_message['session']['trading_day']
                    self.message['trading_day'] = self.trading_day

                self.message['accounts'] = new_message['accounts']['CNY']
                if "WithdrawQuota" not in self.message['accounts'].keys():
                    self.message['accounts']['WithdrawQuota'] =  self.message['accounts']['available']
                self.message['investor_name'] = new_message.get(
                    'investor_name', '')
                for key in ['positions', 'orders', 'banks', 'transfers']:
                    self.updateSinglekey(key, new_message)

                if len(new_message['banks']) >= 1:
                    res = list(new_message['banks'].values())[0]
                    if res['name'] == '':
                        self.ws.send(querybank(
                            self.api_key, self.message['capital_password'], res['id'], self.message['bank_password']))
                    self.message['bankid'] = res['id']
                    self.message['bankname'] = res['name']
                    self.message['money'] = res['fetch_amount']

                try:
                    for tradeid in data[api_key]['trades'].keys():
                        if tradeid not in self.message['trades'].keys():
                            QA.QA_util_log_info('pub transaction')
                            self.pub_transaction.pub(json.dumps(
                                data[api_key]['trades'][tradeid]), routing_key=self.api_key)
                except Exception as e:
                    QA.QA_util_log_info(e)
                self.updateSinglekey('trades', new_message)
                #print('update!!!!!!!!!!!!!!!!!!!!!!')

                self.update_account()

                self.xhistory.insert_one(
                    {'api_key': api_key, 'accounts': self.message['accounts'], 'updatetime': self.last_update_time})

            except Exception as e:
                print(e)
                if 'notify' in message['data'][0]:
                    data = message['data'][0]['notify']
                    mess = list(data.values())[0]['content']
                    typed = list(data.values())[0].get('type', '')
                    self.message['event'][str(datetime.datetime.now())[
                        0:19]] = mess

                    """{'N8': {'type': 'MESSAGE', 'level': 'INFO', 'code': 0, 'content': '登录成功'}"""
                    if '修改密码成功' in mess:
                        self.message['password'] = self.tempPass

                    elif '转账成功' in mess:
                        pass
                    elif '这一时间段不能转账' in mess:
                        pass
                    elif '银行账户余额不足' in mess:
                        pass
                    elif '下单成功' in mess:
                        pass
                    elif '撤单成功' in mess:
                        pass
                    elif '用户登录失败' in mess:
                        self.message['status'] = 600
                        self.update_account()
                        # self.ws.close()
                    elif '登陆成功' in mess:
                        pass

                    self.update_account()
                # QA.QA_util_log_info(data)
        elif message['aid'] == "qry_settlement_info":
            reportdate = message['trading_day']
            self.message['settlement'][str(
                reportdate)] = message['settlement_info']
            # print(self.message)
            self.update_account()

    # 结算?
    def settle(self):
        """配对otg的一个结算过程

        1. 清空并存储历史的文件
        2. 进入settle状态
        3. 晚上进行恢复


        1. 重置账户(update('$set': {})
        2. 发送结算信息 ==> wechat id
        3. 保存历史的持仓/最后一个市值等
        4. 
        """
        #print(self.message)
        #self.message['trading_day'] = str(self.message['updatetime'])[0:10]
        self.settle_client.update_one({'api_key': self.api_key, 'trading_day': self.message.get('trading_day', str(self.last_update_time)[0:10])}, {
            '$set': fix_dict(self.message)}, upsert=True)
        """
        暂时注释
        self.message['positions'] = []
        self.message['orders'] = []
        self.message['trades'] = []
        self.account_client.update_one({'api_key': self.api_key}, {
            '$set': fix_dict(self.message)}, upsert=True)
        """
        pass

    def update(self, old, new):

        for item in new.keys():
            old[item] = new[item]
        return old

    # 作为subscriber的接受函数
    def callback(self, a, b, c, body):
        """
        格式为json的str/bytes
        字段:
        {
            api_key
            order_direction {str} -- [description] (default: {'BUY'})
            order_offset {str} -- [description] (default: {'OPEN'})
            volume {int} -- [description] (default: {1})
            order_id {bool} -- [description] (default: {False})
            code {str} -- [description] (default: {'rb1905'})
            exchange_id {str} -- [description] (default: {'SHFE'})
        }
        """
        def targs():
            z = json.loads(str(body, encoding='utf-8'))
            QA.QA_util_log_info('===================== \n RECEIVE')
            QA.QA_util_log_info(z)

            if z['topic'] == 'sendorder':
                params = [
                  {"instrument_id": z.get('code', ''),
                   "side": z.get('order_direction', 'buy'),
                   "type": "limit", #"market"
                   "price": z.get('price', 0),
                   "size": z.get('volume', 1)},
                  # {"instrument_id": "XRP-USDT", "side": "buy", "type": "market", "price": "2.5451", "notional": "1"}
                ]
                result = self.spotAPI.take_orders(params)
                print("sendorder",result)
                self.orders_client.insert_one(z)

            elif z['topic'] == 'peek':
                #
                self.ws.send(peek())
            elif z['topic'] == 'subscribe':
                #监听:
                self.ws.send(
                    subscribe_quote())
            elif z['topic'] == 'cancel_order':
                result = spotAPI.revoke_order(z['code'], order_id=z['order_id'])
            elif z['topic'] == 'transfer':
                # # 转账...
                # self.ws.send(
                #     transfer(z['api_key'], z.get('capital_password', self.message['capital_password']),
                #              z.get('bankid', self.message['bankid']), z.get('bankpassword', self.message['bank_password']), z['amount'])
                # )
                # self.message['banks'][z.get(
                #     'bankid', self.message['bankid'])]['fetch_amount'] = -1
                # self.ws.send(
                #     querybank(z['api_key'], z.get('capital_password', self.message['capital_password']),
                #               z.get('bankid', self.message['bankid']), z.get('bankpassword', self.message['bank_password']))
                # )
                pass
            elif z['topic'] == 'query_bank':

                # x = list(self.message['banks'].())[0]
                # x['fetch_amount'] = -1
                self.message['banks'][z.get(
                    'bankid', self.message['bankid'])]['fetch_amount'] = -1
                self.ws.send(
                    querybank(z['api_key'], z.get('capital_password', self.message['capital_password']),
                              z.get('bankid', self.message['bankid']), z.get('bankpassword', self.message['bank_password']))
                )
            elif z['topic'] == 'kill':
                self.message['status'] = 500
                self.update_account()
                self.settle()
                self.ws.close()
                raise Exception
            elif z['topic'] == 'query_settlement':
                #self.ws.send(query_settlement(int(z.get('day'))))
                pass
            elif z['topic'] == 'change_password':
                # self.ws.send(
                #     change_password(self.message['password'], z['newPass'])
                # )
                # self.tempPass = z['newPass']
                pass

        threading.Thread(target=targs, name='callback_handler',
                         daemon=True).start()
