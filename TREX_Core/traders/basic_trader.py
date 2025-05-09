# from _clients.participants.participants import Residential

import asyncio
# import serialize
import TREX_Core.utils as utils

class Trader:
    def __init__(self, **kwargs):
        # Some util stuffies
        self.participant = kwargs['context']

        # Initialize the agent learning parameters for the agent (your choice)
        self.bid_price = kwargs['bid_price'] if 'bid_price' in kwargs else None
        self.ask_price = kwargs['ask_price'] if 'ask_price' in kwargs else None
        self.action_scenario_history = {}
    # Core Functions, learn and act, called from outside

    async def act(self, **kwargs):
        actions = {}
        last_settle = self.participant.timing['last_settle']
        next_settle = self.participant.timing['next_settle']
        timezone = self.participant.timing['timezone']
        next_settle_end = utils.timestamp_to_local(next_settle[1], timezone)
        charge_hours_allowed = (8, 9, 10, 11, 12, 13, 14, 15, 16)

        generation, load = await self.participant.read_profile(next_settle)
        residual_load = load - generation
        residual_gen = -residual_load

        if self.participant.storage is not None:
            # print('e-1.1')
            storage_schedule = await self.participant.storage.check_schedule(next_settle)
            max_charge = storage_schedule[next_settle]['energy_potential'][1]
            max_discharge = storage_schedule[next_settle]['energy_potential'][0]

            # adjust battery actions from last settle

            if last_settle in self.action_scenario_history:
                last_settle_info = await self.participant.ledger.get_settled_info(last_settle)
                # s1: charge settled max(0, bids - residual load)
                # s2: discharge residual load + settled asks
                # s3: change settled bids + residual gen
                # s4: discharge max(0, settled asks - residual gen)

                if self.action_scenario_history[last_settle]['scenario'] == 1:
                    ls_bids = last_settle_info['bids']['quantity']
                    ls_residual_load = self.action_scenario_history[last_settle]['residual_load']
                    ls_max_charge = self.action_scenario_history[last_settle]['max_charge']
                    actions['bess'] = {
                        str(last_settle): min(ls_max_charge, max(0, ls_bids - ls_residual_load))
                    }
                elif self.action_scenario_history[last_settle]['scenario'] == 2:
                    ls_asks = last_settle_info['asks']['quantity']
                    ls_residual_load = self.action_scenario_history[last_settle]['residual_load']
                    ls_max_discharge = self.action_scenario_history[last_settle]['max_discharge']
                    actions['bess'] = {
                        str(last_settle): -min(abs(ls_max_discharge), (ls_residual_load + ls_asks))
                    }
                elif self.action_scenario_history[last_settle]['scenario'] == 3:
                    ls_bids = last_settle_info['bids']['quantity']
                    ls_residual_gen = self.action_scenario_history[last_settle]['residual_gen']
                    ls_max_charge = self.action_scenario_history[last_settle]['max_charge']
                    actions['bess'] = {
                        str(last_settle): min(ls_max_charge, ls_bids + ls_residual_gen)
                    }
                elif self.action_scenario_history[last_settle]['scenario'] == 4:
                    ls_asks = last_settle_info['asks']['quantity']
                    ls_residual_gen = self.action_scenario_history[last_settle]['residual_gen']
                    ls_max_discharge = self.action_scenario_history[last_settle]['max_discharge']
                    actions['bess'] = {
                        str(last_settle): -min(abs(ls_max_discharge), max(0, ls_asks - ls_residual_gen))
                    }
                # clean up history buffer
                stale_round = self.participant.timing['stale_round']
                self.action_scenario_history.pop(stale_round, None)

        # if battery not full, and allowed to charge, add max charge potential to bid quantity
        if residual_load > 0:
            if self.participant.storage is not None:
                if next_settle_end.hour in charge_hours_allowed:
                    actions['bids'] = {
                        str(next_settle): {
                            'quantity': residual_load + max_charge,
                            'price': self.bid_price
                        }
                    }
                    self.action_scenario_history[next_settle] = {
                        'scenario': 1,
                        'residual_load': residual_load,
                        'residual_gen': residual_gen,
                        'max_charge': max_charge,
                        'max_discharge': max_discharge
                    }
                else:
                    actions['asks'] = {
                        'bess': {
                            str(next_settle): {
                                'quantity': max(0, max_discharge - residual_load),
                                'price': self.ask_price
                            }
                        }
                    }
                    self.action_scenario_history[next_settle] = {
                        'scenario': 2,
                        'residual_load': residual_load,
                        'residual_gen': residual_gen,
                        'max_charge': max_charge,
                        'max_discharge': max_discharge
                    }
            else:
                # if were lacking energy, try to get difference from market
                actions['bids'] = {
                    str(next_settle): {
                        'quantity': residual_load,
                        'price': self.bid_price
                    }
                }

        # if we have too much, cram as much as possible into battery
        elif residual_gen > 0:
            if self.participant.storage is not None:
                if next_settle_end.hour in charge_hours_allowed:
                    actions['bids'] = {
                        str(next_settle): {
                            'quantity': max(0, max_charge - residual_gen),
                            'price': self.bid_price
                        }
                    }
                    self.action_scenario_history[next_settle] = {
                        'scenario': 3,
                        'residual_load': residual_load,
                        'residual_gen': residual_gen,
                        'max_charge': max_charge,
                        'max_discharge': max_discharge
                    }
                else:
                    actions['asks'] = {
                        'solar': {
                            str(next_settle): {
                                'quantity': residual_gen,
                                'price': self.ask_price
                            }
                        },
                        'bess': {
                            str(next_settle): {
                                'quantity': max_discharge,
                                'price': self.ask_price
                            }
                        }
                    }
                    self.action_scenario_history[next_settle] = {
                        'scenario': 4,
                        'residual_load': residual_load,
                        'residual_gen': residual_gen,
                        'max_charge': max_charge,
                        'max_discharge': max_discharge
                    }
            else:
                # if were lacking energy, try to get difference from market
                actions['asks'] = {
                    'solar': {
                        str(next_settle): {
                            'quantity': residual_gen,
                            'price': self.ask_price
                        }
                    }
                }


        return actions

    async def step(self):
        next_actions = await self.act()
        return next_actions

    async def reset(self, **kwargs):
        self.action_scenario_history.clear()
        return True