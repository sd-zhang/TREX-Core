import asyncio
import json
import os
import signal
from TREX_Core.mqtt.base_gmqtt import BaseMQTTClient


class Client(BaseMQTTClient):
    def __init__(self, host, port, market_configs):
        super().__init__(host, port, consumers=4)
        market_configs = market_configs
        market_configs['market_id'] = market_configs.pop('id', '')
        grid_params = market_configs.pop('grid', {})

        try:
            market = importlib.import_module('markets.' + market_configs['type']).Market
        except ImportError:
            market = importlib.import_module('TREX_Core.markets.' + market_configs['type']).Market

        self.market = market(client=self.client,
                             **market_configs,
                             grid_params=grid_params)

        market_id = self.market.market_id
        self.SUBS = [
            (f'{market_id}', 2),
            (f'{market_id}/join_market/+', 2),
            (f'{market_id}/{market_id}', 2),
            (f'{market_id}/bid', 2),
            (f'{market_id}/ask', 2),
            (f'{market_id}/settlement_delivered', 2),
            (f'{market_id}/meter', 2),
            (f'{market_id}/simulation/start_round', 2),
            (f'{market_id}/simulation/start_episode', 2),
            (f'{market_id}/simulation/end_episode', 2),
            (f'{market_id}/simulation/end_simulation', 2),
            (f'{market_id}/simulation/is_market_online', 2),
        ]

        self.dispatch = {
            "join_market":          self.on_participant_connected,
            "bid":                  self.on_bid,
            "ask":                  self.on_ask,
            "settlement_delivered": self.on_settlement_delivered,
            "meter":                self.on_meter_data,
            "start_round":          self.on_start_round,
            "start_episode":        self.on_start_episode,
            "end_episode":          self.on_end_episode,
            "end_simulation":       self.on_end_simulation,
            "is_market_online":     self.on_is_market_online,
        }

        self.data_recorded = False
        self.recording_complete = False
        self.msg_queue = asyncio.Queue()

    def on_connect(self, client, flags, rc, properties):
        self.subscribe_common(client)
        print('Connected market', self.market.market_id)

    def on_disconnect(self, client, packet, exc=None):
        # self.market.server_online = False
        print('market disconnected')

    async def on_bid(self, message):
        bid = json.loads(message['payload'])
        try:
            entry_id, participant_id, participant_sid = await self.market.submit_bid(bid)
            self.client.publish(f'{self.market.market_id}/{participant_id}/bid_ack', entry_id,
                                user_property=[('to', participant_sid)],
                                qos=1)
        except TypeError:
            return

    async def on_ask(self, message):
        ask = json.loads(message['payload'])
        try:
            entry_id, participant_id, participant_sid = await self.market.submit_ask(ask)
            self.client.publish(f'{self.market.market_id}/{participant_id}/ask_ack', entry_id,
                                user_property=[('to', participant_sid)],
                                qos=1)
        except TypeError:
            return

    async def on_settlement_delivered(self, message):
        payload = json.loads(message['payload'])
        await self.market.settlement_delivered(payload)

    async def on_meter_data(self, message):
        # print("meter data")
        payload = json.loads(message['payload'])
        await self.market.meter_data(payload)

    async def on_participant_connected(self, message):
        # print(type(client_data))
        if message['payload']:
            client_data = json.loads(message['payload'])
            client_data['id'] = message['topic'].split('/')[-1]
            market_id, market_sid, timezone = await self.market.participant_connected(client_data)
            self.client.publish(f'{self.market.market_id}/{client_data['id']}/market_info',
                                {'id': market_id,
                                 'sid': market_sid,
                                 'timezone': timezone, },
                                user_property=[('to', client_data['sid'])],
                                qos=2)

    async def on_is_market_online(self, message):
        self.client.publish(f'{self.market.market_id}/simulation/market_online', self.market.market_id, qos=1)

    async def on_start_round(self, message):
        if self.market.round_in_progress:
            return
        self.market.round_in_progress = True
        payload = json.loads(message['payload'])
        step_task = asyncio.create_task(self.market.step(payload['duration'], sim_params=payload))
        step_task.add_done_callback(self.on_round_done)

    def on_round_done(self, task):
        self.market.round_in_progress = False
        try:
            task_result = task.result()
        except Exception as exc:
            # 2️⃣ log the hidden exception so you know why the round died
            print(f"[market] step() crashed: {exc!r}")
        self.client.publish(f'{self.market.market_id}/simulation/end_round', self.market.market_id, qos=1)

    async def on_start_episode(self, message):
        table_name = f'{message['payload']}_{self.market.market_id}'
        await self.market.open_db(table_name)

    async def on_end_episode(self, message):
        await self.market.ensure_transactions_complete()
        await self.market.reset_market()
        self.client.publish(f'{self.market.market_id}/simulation/market_ready', self.market.market_id, qos=1)

    async def on_end_simulation(self, message):
        self.market.run = False
        await self.market.ensure_transactions_complete()
        await self.market.close_connection()
        await self.client.disconnect()
        os.kill(os.getpid(), signal.SIGINT)
        raise SystemExit

    async def run(self):
        await super().run()


if __name__ == '__main__':
    import argparse
    import importlib

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--host', default="localhost", help='')
    parser.add_argument('--port', default=1883, help='')
    parser.add_argument('--configs')
    args = parser.parse_args()

    client = Client(host=args.host,
                    port=args.port,
                    market_configs=json.loads(args.configs))

    try:
        import uvloop
        uvloop.run(client.run())
    except ImportError:
        asyncio.run(client.run())
