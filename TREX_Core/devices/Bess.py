# battery mode (precharge (up to 10% capacity), Current mode (10 to 80%), CV (80 to 100%))
# settings (capacity, max current)
# charge algorithm (constant current, variable current, etc)

# BATTERY MODELS
# https://www.mathworks.com/help/physmod/sps/powersys/ref/battery.html#bry4req-2
# https://github.com/susantoj/kinetic-battery/blob/master/kinetic_battery.py
# https://www.homerenergy.com/products/pro/docs/3.11/creating_an_idealized_storage_component.html

# TODO: revamp status to give the SoC at the beginning and end of the current time step
# TODO: simplify scheduling to the net energy activity of the battery (assuming constant power, within max bounds)
# TODO: add functions to check schedule
# TODO: at some point make sure all user submitted time intervals are evenly divisible by round duration

class Storage:
    """This is an idealized battery model to emulate a Li-ion or Li-Po battery

    Battery has an internal, schedule based controller. Total energy to be charged or discharged for a time interval is given to the controller ahead of time. Assumes contant voltage and current for charge/discharge control.

    # battery control is scheduled as net charge for the time interval
    # positive charge = charge
    # negative charge = discharge
    # schedule is a dictionary. Key is the timestamp intervals (unix time) as a tuple
    # value of key is a list. first element in list is charge. second element in list is discharge
    # net energy is physically limited
    # for fixed interval market timings this should temporally align.
    # a bit more work needs to be put in to make scheduling more dynamic
    # example:
    # self.__schedule = {
    #     (0, 60): 50,
    #     (60, 120): -50
    # }

    # efficiency is one way efficiency. Round trip efficiency is efficiecy^2
    # standard units used are:
    # power: W; Energy: Wh; time: s
    # self discharge is assumed to be a constant.

    # charge and discharge are measured externally at the meter, which means that 
    # internally it will charge less than metered and discharge more than metered due to non-ideal efficiency

    Returns:
        [type]: [description]
    """

    def __init__(self, capacity=7000, power=3300, efficiency=0.95, monthly_sdr=0.05):
        self.timing = dict()

        # Capacity is usable capacity. Limited to the linear region in the C/D curve
        # actual capacity might be larger but it is irrelevant
        self.__info = {
            'capacity': capacity,  # Wh
            'efficiency': efficiency,  # charges less than meter readings, and discharges more than meter readings
            'power_rating': power,  # reading at the meter in W
            'self_discharge_rate_min': capacity * monthly_sdr / 28 / 24 / 60,
            # fixed quantity in Wh based on full capacity
            'remaining_energy': 0,  # Wh
            'state_of_charge': 0,  # Wh
        }

        self.__schedule = {}
        self.__last_round_processed = None
        self.last_activity = 0

    # Collect status of battery charge at start and end of turn/ current scheduled battery charge or discharge
    async def __status(self, **kwargs):
        current_energy_activity = 0
        if self.timing['current_round'] in self.__schedule:
            # get scheduled energy activity (should be happening right now)
            current_energy_activity = self.__schedule[self.timing['current_round']]
        if self.__last_round_processed != self.timing['current_round']:
            # Predict soc at end time step if round has not been processed yet
            soc_start = self.__info['remaining_energy']
            soc_end = max(0, soc_start + current_energy_activity - self.__info['self_discharge_rate_min'])
        else:
            # Once round has been processed, use soc end to calculate soc start
            soc_end = self.__info['remaining_energy']
            soc_start = soc_end - current_energy_activity + self.__info['self_discharge_rate_min']
        if 'internal' in kwargs:
            self.__info['remaining_energy'] = soc_end
            self.__info['state_of_charge'] = round(soc_end/self.__info['capacity'], 1)
            # return (soc_start, current_energy_activity, soc_end)
        return soc_start, current_energy_activity, round(soc_end, 2)

    # Check that time interval is a multiple of time durations
    def __time_interval_is_valid(self, time_interval: tuple):
        # print(self.timing)
        duration = self.timing['duration']
        if (time_interval[1] - time_interval[0]) % duration != 0:
            # make sure duration is a multiple of round duration
            return False
        if time_interval[0] % duration != 0:
            return False
        if time_interval[1] % duration != 0:
            return False
        return True

    # Check scheduled battery charge or discharge
    async def check_schedule(self, time_interval: tuple):
        if not self.__time_interval_is_valid(time_interval):
            print('invalid interval')
            return False

        if self.timing['current_round'][0] is None:
            return False

        round_duration = self.timing['duration']
        meter_potential = self.__info['power_rating'] * (round_duration / 3600)
        schedule = {}

        # charge cap and discharge cap are at the meter
        elapse_blocks = int((time_interval[1] - self.timing['current_round'][0]) / round_duration)
        elapse_intervals = [(self.timing['current_round'][0] + s * round_duration,
                             self.timing['current_round'][0] + (s + 1) * round_duration) for s in range(elapse_blocks)]
        request_intervals = elapse_intervals[
                        elapse_intervals.index((time_interval[0], time_interval[0] + round_duration)):]

        request_blocks = len(request_intervals)
        soc_start = self.__info['remaining_energy']
        for idx in range(request_blocks):
            elapse_keys = elapse_intervals[:elapse_intervals.index(request_intervals[idx]) + 1]
            elapsed_and_scheduled = {key: self.__schedule[key] for key in elapse_keys if key in self.__schedule}
            total_scheduled_use = sum(elapsed_and_scheduled.values())
            projected_self_discharge = len(elapse_keys) * self.__info['self_discharge_rate_min']
            projected_energy = max(0, min(self.__info['capacity'],
                                       soc_start + total_scheduled_use - projected_self_discharge))
            charge_cap = (self.__info['capacity'] - projected_energy) / self.__info['efficiency']  # at the meter
            discharge_cap = projected_energy * self.__info['efficiency']  # at the meter

            max_charge = min(charge_cap, meter_potential)
            max_discharge = min(discharge_cap, meter_potential)
            scheduled_step = self.__schedule[request_intervals[idx]] if request_intervals[idx] in self.__schedule else 0

            schedule[request_intervals[idx]] = {
                'energy_potential': (-int(max_discharge), int(max_charge)),
                'projected_energy_end': round(projected_energy, 2),
                'projected_soc_end': round(projected_energy/self.__info['capacity'], 2),
                'energy_scheduled': scheduled_step
            }
        return schedule

    # Schedule for buying or selling of energy at given time interval
    async def schedule_energy(self, energy: int, time_interval: tuple):
        """Function used to schedule charge or discharge

        # energy is the amount to be charged or discharged in Wh
        # positive energy means charge
        # negative energy means discharge

        Time duration is limited to 60s for now.

        Args:
            energy (int): [description]
            time_interval (tuple): [description]

        Returns:
            [type]: [description]
        """
        if not self.__time_interval_is_valid(time_interval):
            return False

        # TODO: only deal with intervals of 60s for now.
        if energy == 0:
            self.__schedule.pop(time_interval, None)
            return 0

        schedule = await self.check_schedule(time_interval)
        if energy > 0:
            self.__schedule[time_interval] = min(energy, schedule[time_interval]['energy_potential'][1])
        elif energy < 0:
            self.__schedule[time_interval] = max(energy, schedule[time_interval]['energy_potential'][0])
        return self.__schedule[time_interval]

    # Process scheduled energy for current time step
    async def __process_schedule(self):
        """ process charge or discharge for the current time step, as scheduled
        Also handles self discharge

        Returns:
            [type]: [description]
        """

        if self.__last_round_processed == self.timing['current_round']:
            return False
        soc_start, energy_activity, soc_end = await self.__status(internal=True)
        # _, _, self.__info['remaining_energy'] = await self.__status(internal=True)
        self.__schedule.pop(self.timing['last_round'], None)
        self.__last_round_processed = self.timing['current_round']
        self.last_activity = energy_activity
        return soc_start, energy_activity, soc_end

    async def step(self):
        return await self.__process_schedule()

    def get_info(self, *args):
        if args:
            return {k: self.__info[k] for k in args}
        return self.__info

    def reset(self, soc_pct):
        remaining_energy = int(self.__info['capacity'] * max(0, min(1, soc_pct/100)))
        self.__info['remaining_energy'] = remaining_energy
        self.__info['state_of_charge'] = soc_pct
        self.__schedule = {}
        self.__last_round_processed = None
        self.last_activity = 0