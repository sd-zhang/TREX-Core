import ast
import importlib
import json

import databases
import tenacity

from _clients.participants import ledger
from _utils import db_utils, utils
import os
import signal


class Participant:
    """
    Participant is the interface layer between local resources and the Market
    """
    def __init__(self, sio_client, participant_id, market_id, db_path, trader_params, storage_params, **kwargs):
        # Initialize participant variables
        self.server_online = False
        self.run = True
        self.market_id = market_id
        self.market_connected = False
        self.participant_id = str(participant_id)
        self.__client = sio_client
        self.client = sio_client
        self.__profile = {
            'db_path': db_path
        }

        # Initialize market variables
        self.__ledger = ledger.Ledger(self.participant_id)
        self.__extra_transactions = {}
        self.__market_info = {}
        self.__meter = {}
        self.__timing = {}

        # Initialize trader variables and functions
        trader_params = json.loads(trader_params)
        trader_fns = {
            'id': self.participant_id,
            'timing': self.__timing,
            'ledger': self.__ledger,
            'extra_transactions': self.__extra_transactions,
            'market_info': self.__market_info,
            'read_profile': self.__read_profile,
            'get_profile_stats': self.__get_profile_stats,
            'meter': self.__meter
        }

        if storage_params:
            storage_params = json.loads(storage_params)
            storage_type = storage_params.pop('type', None)
            # self.storage_fns = {
            #     'id': self.participant_id,
            #     'timing': self.__timing
            # }
            self.storage = importlib.import_module('_devices.' + storage_type).Storage(**storage_params)
            self.storage.timing = self.__timing
            trader_fns['storage'] = {
                'info': self.storage.get_info,
                'check_schedule': self.storage.check_schedule,
                # 'schedule_energy': self.storage.schedule_energy
            }

        trader_type = trader_params.pop('type', None)
        if trader_type == 'remote_agent':
            trader_fns['emit'] = self.__client.emit
        Trader = importlib.import_module('_agent.traders.' + trader_type).Trader
        self.trader = Trader(trader_fns=trader_fns, **trader_params)

        self.__profile_params = {
            'generation_scale': kwargs['generation_scale'] if 'generation_scale' in kwargs else 1,
            'load_scale': kwargs['load_scale'] if 'load_scale' in kwargs else 1
        }
        trader_fns['profile_params'] = self.__profile_params
        synthetic_profile = trader_params.pop('use_synthetic_profile', None)
        if synthetic_profile:
            self.__profile_params['synthetic_profile'] = synthetic_profile

        # if 'market_ns' in kwargs:
        #     NSMarket = importlib.import_module(kwargs['market_ns']).NSMarket
            # self.__client.register_namespace(NSMarket(self))

    async def delay(self, s):
        await self.__client.sleep(s)

    # async def disconnect(self):
    #     '''
    #     This method disconnects the client from the server
    #     '''
    #     await self.__client.disconnect()

    async def open_db(self, db_path):
        """Opens connection to the database where load and generation profiles are stored.
        Also stores references to the database object and the table object

        Args:
            db_path ([type]): [description]
        """
        self.__profile['db_path'] = db_path
        self.__profile['db'] = databases.Database(db_path)
        profile_name = self.__profile_params['synthetic_profile'] if 'synthetic_profile' in self.__profile_params\
            else self.participant_id
        self.__profile['name'] = profile_name
        self.__profile['db_table'] = db_utils.get_table(db_path, profile_name)
        if 'db_table' in self.__profile or self.__profile['db_table'] is not None:
            await self.__profile['db'].connect()

    async def __get_profile_stats(self):
        """reads and returns pre-calculated profile statistics for calculating Z scores, if available.
        """
        db = self.__profile['db']
        table = db_utils.get_table(self.__profile['db_path'], "_statistics")
        query = table.select().where(table.c.name == self.__profile['name'])
        # async with db.transaction():
        row = await db.fetch_one(query)
        if row is not None:
            return dict(row)
        return None

    async def open_profile_db(self):
        await self.open_db(self.__profile['db_path'])
        # await self.get_profile_stats(self.__profile['db_path'])

    async def join_market(self):
        """Emits event to join a Market
        """
        client_data = {
            'type': ('participant', 'Residential'),
            'id': self.participant_id,
            'market_id': self.market_id
        }
        await self.__client.emit('join_market', client_data, callback=self.register_success)

    # Continuously attempt to join server
    async def register_success(self, success):
        if not success:
            # self.__profiles_available()
            await self.delay(3)
            await self.join_market()
        self.busy = False

    async def update_extra_transactions(self, message):
        time_delivery = tuple(message.pop('time_delivery'))
        self.__ledger.extra[time_delivery] = message
        self.__extra_transactions.clear()
        self.__extra_transactions.update(message)

    @tenacity.retry(wait=tenacity.wait_random(0, 3))
    async def bid(self, time_delivery=None, **kwargs):
        """Submit a bid

        Args:
            time_delivery ([type], optional): [description]. Defaults to None.
        """

        # quantity is energy in Wh
        # price is $/kWh
        if time_delivery is None:
            time_delivery = self.__timing['next_settle']

        bid_entry = {
            'participant_id': self.participant_id,
            'quantity': kwargs['quantity'],  # Wh
            'price': kwargs['price'],  # $/kWh
            'time_delivery': time_delivery
        }
        # print('bidding', self.trader.is_learner, self.__timing, bid_entry)
        await self.__client.emit('bid', bid_entry)

    @tenacity.retry(wait=tenacity.wait_random(0, 3))
    async def ask(self, time_delivery=None, **kwargs):
        """Submit an ask

        Args:
            time_delivery ([type], optional): [description]. Defaults to None.
        """
        # quantity is energy in Wh
        # price is $/kWh
        if time_delivery is None:
            time_delivery = self.__timing['next_settle']

        ask_entry = {
            'participant_id': self.participant_id,
            'quantity': kwargs['quantity'],  # Wh
            'price': kwargs['price'],  # $/kWh
            'source': kwargs['source'],
            'time_delivery': time_delivery
        }
        await self.__client.emit('ask', ask_entry)

    async def ask_success(self, message):
        await self.__ledger.ask_success(message)

    async def bid_success(self, message):
        await self.__ledger.bid_success(message)

    async def settle_success(self, message):
        await self.__ledger.settle_success(message)
        return message['commit_id']

    async def __update_time(self, message):
        # synchronizes time with market
        duration = message['duration']
        start_time = message['time']
        self.__timing.update({
            'timezone': message['timezone'],
            'duration': duration,
            'last_round': tuple(message['last_round']),
            'current_round': tuple(message['current_round']),
            'last_settle': tuple(message['last_settle']),
            'next_settle': tuple(message['next_settle']),
            'stale_round': (start_time - duration * 10, start_time - duration * 9)
        })


    async def start_round(self, message):
        """Sequence of actions during each round
        Currently only for simulation mode.
        Real time mode needs slight modifications.

        Args:
            message ([type]): [description]
        """
        # start of current time step
        await self.__update_time(message)
        self.__market_info.update(message['market_info'])
        await self.__ledger.clear_history(self.__timing['stale_round'])
        self.__market_info.pop(str(self.__timing['stale_round']), None)
        # print(self.__market_info)
        # agent_act tells what actions controller should perform
        # controller should perform those actions accordingly, but will have the option not to
        next_actions = await self.trader.step()
        # next_actions = await self.trader.act()
        await self.__take_actions(next_actions)
        # await self.trader.learn()
        if hasattr(self, 'storage'):
            await self.storage.step()

        # metering energy should happen right at the end of the current round for maximum accuracy
        # in real-time mode there would have to be a timeout function
        # this is currently OK for simulation mode
        await self.__meter_energy(self.__timing['current_round'])
        # await self.__client.emit('end_turn', namespace='/market')
        await self.__client.emit('end_turn')

    async def __read_profile(self, time_interval):
        """Fetches energy profile for one timestamp from database

        Args:
            time_interval ([type]): [description]

        Returns:
            [type]: [description]
        """
        db = self.__profile['db']
        table = self.__profile['db_table']
        # query = table.select().where(table.c.tstamp == time_interval[1])
        query = table.select().where(table.c.time == time_interval[1])
        async with db.transaction():
            row = await db.fetch_one(query)
        return utils.process_profile(row=row,
                                     gen_scale=self.__profile_params['generation_scale'],
                                     load_scale=self.__profile_params['load_scale'])

    # def __process_profile(self, row):
    #     """Processes raw readings fetches from database into generation and consumption in integer Wh.
    #
    #     Also scales if scaling is defined in configuration.
    #     Right now the format is for eGauge. Functionality will be expanded as more data sources are introduced.
    #
    #     Args:
    #         row ([type]): [description]
    #
    #     Returns:
    #         [type]: [description]
    #     """
    #
    #     if row is not None:
    #         consumption = int(round(self.__profile_params['load_scale'] * (row['grid'] + row['solar+']), 0))
    #         generation = int(round(self.__profile_params['generation_scale'] * row['solar+'], 0))
    #         return generation, consumption
    #     return 0, 0

    async def __meter_energy(self, time_interval):
        """Sends submetering data to the Market

        In simulation mode, the data is sent at the end of the current step
        In real-time mode, the data is sent at the beginning of the next step

        Args:
            time_interval ([type]): [description]

        Returns:
            [type]: [description]
        """

        if not self.server_online:
            return False

        if not self.market_connected:
            return False

        self.__meter = await self.__allocate_energy(time_interval)
        await self.__client.emit('meter_data', self.__meter)
        return True

    async def __allocate_energy(self, time_interval):
        """
        This function performs virtual sub metering.
        energy generated is allocated to sources by priority:
        1. settlements
        2. self consumption
        battery, then
        other loads
        3. grid
        """

        self.__timing['last_deliver'] = time_interval
        meter = {
            'time_interval': time_interval,

            # generation is NET from each source
            # self consumed energy are allocated to consumption
            # this makes market code more efficient
            'generation': {
                'solar': 0,
                'bess': 0
            },
            'consumption': {
                # consumption keeps track of self consumption by source
                # other denotes energy flowing in from the outside
                'bess': {
                    'solar': 0  # solar from self
                },
                'other': {
                    'solar': 0,  # solar from self
                    'bess': 0,  # bess from self
                    'external': 0
                }
            }
        }

        # step 1. gather settlements
        settled_solar = 0
        settled_bess = 0
        if time_interval in self.__ledger.settled:
            asks = self.__ledger.settled[time_interval]['asks']

            for ask in asks.values():
                if ask['source'] == 'solar':
                    settled_solar += ask['quantity']
                if ask['source'] == 'bess':
                    settled_bess += ask['quantity']

        # step 2. get battery activity
        bess_charge = 0
        bess_discharge = 0

        if hasattr(self, 'storage'):
            # bess_activity = self.storage.last_activity
            bess_activity = await self.storage.check_schedule(time_interval)
            bess_activity = bess_activity[time_interval]['energy_scheduled']
            bess_charge = bess_activity if bess_activity > 0 else 0
            bess_discharge = -bess_activity if bess_activity < 0 else 0

        # step 3. get readings from meter (profile)
        solar_generation, residual_consumption = await self.__read_profile(time_interval)

        # step 4. allocate energy
        # use solar for settlement
        residual_solar = max(0, solar_generation - settled_solar)
        if residual_solar == 0:
            meter['generation']['solar'] += solar_generation
        else:
            meter['generation']['solar'] += settled_solar

        # use residual solar to charge battery (if battery was charged)
        if residual_solar > 0 and bess_charge > 0:
            if residual_solar <= bess_charge:
                meter['consumption']['bess']['solar'] += residual_solar
                bess_charge -= residual_solar
                residual_solar -= residual_solar
            elif residual_solar > bess_charge:
                meter['consumption']['bess']['solar'] += bess_charge
                residual_solar -= bess_charge
                bess_charge -= bess_charge

        # use residual solar for other loads
        if residual_solar > 0:
            if residual_solar <= residual_consumption:
                meter['consumption']['other']['solar'] += residual_solar
                residual_consumption -= residual_solar
                residual_solar -= residual_solar
            elif residual_solar > residual_consumption:
                meter['consumption']['other']['solar'] += residual_consumption
                residual_solar -= residual_consumption
                residual_consumption -= residual_consumption

        # if battery was discharged
        if bess_discharge > 0:
            if bess_discharge <= residual_consumption:
                meter['consumption']['other']['bess'] += bess_discharge
                residual_consumption -= bess_discharge
                bess_discharge -= bess_discharge
            elif bess_discharge > residual_consumption:
                meter['consumption']['other']['bess'] += residual_consumption
                bess_discharge -= residual_consumption
                residual_consumption -= residual_consumption

        meter['generation']['solar'] += residual_solar
        meter['generation']['bess'] += bess_discharge
        meter['consumption']['other']['external'] += residual_consumption
        meter['consumption']['other']['external'] += bess_charge

        return meter

    async def __take_actions(self, actions):
        """Processes the actions given by the agent

        Args:
            actions ([type]): [description]
        """
        # actions must come in the following format:
        # actions = {
        #     'bess': {
        #         time_interval: scheduled_qty
        #     },
        #     'bids': {
        #         time_interval: {
        #             'quantity': qty,
        #             'price': dollar_per_kWh
        #         }
        #     },
        #     'asks' {
        #         source: {
        #             time_interval: {
        #                 'quantity': qty,
        #                 'price': dollar_per_kWh?
        #             }
        #         }
        #     }
        # }

        # Battery charging or discharging action
        if 'bess' in actions and hasattr(self, 'storage'):
            for time_interval in actions['bess']:
                await self.storage.schedule_energy(actions['bess'][time_interval], ast.literal_eval(time_interval))
        # Bid for energy
        if 'bids' in actions:
            for time_interval in actions['bids']:
                quantity = actions['bids'][time_interval]['quantity']
                price = round(actions['bids'][time_interval]['price'], 4)
                await self.bid(quantity=quantity,
                               price=price,
                               time_delivery=ast.literal_eval(time_interval))
        # Ask to sell energy
        if 'asks' in actions:
            for source in actions['asks']:
                for time_interval in actions['asks'][source]:
                    quantity = actions['asks'][source][time_interval]['quantity']
                    price = round(actions['asks'][source][time_interval]['price'], 4)
                    await self.ask(quantity=quantity,
                                   price=price,
                                   source=source,
                                   time_delivery=ast.literal_eval(time_interval))

    def reset(self):
        self.__ledger.reset()
        self.__extra_transactions.clear()
        self.__market_info.clear()
        self.__meter.clear()
        self.__timing.clear()

    # Keep participant connected to server
    async def ping(self):
        while self.run:
            await self.delay(10)
            if self.server_online:
                await self.__client.emit('ping')
            continue
        await self.__client.sleep(5)
        await self.__client.disconnect()
        os.kill(os.getpid(), signal.SIGINT)
        # raise SystemExit


# import socketio
# class NSMarket(socketio.AsyncClientNamespace):
#     def __init__(self, participant):
#         super().__init__(namespace='/market')
#         self.participant = participant
#
#     # async def on_connect(self):
#     #     await self.participant.join_market()
#
#     # async def on_disconnect(self):
#     #     pass
#
#     async def on_re_register_participant(self, message):
#         await self.participant.join_market()
#
#     async def on_update_market_info(self, market_id):
#         if market_id == self.participant.market_id:
#             self.participant.market_connected = True
#
#     async def on_start_round(self, message):
#         await self.participant.start_round(message)
#
#     async def on_ask_success(self, message):
#         await self.participant.ask_success(message)
#
#     async def on_bid_success(self, message):
#         await self.participant.bid_success(message)
#
#     async def on_settled(self, message):
#         return await self.participant.settle_success(message)
#
#     async def on_return_extra_transactions(self, message):
#         await self.participant.update_extra_transactions(message)
#
# if __name__ == '__main__':
#     pass
