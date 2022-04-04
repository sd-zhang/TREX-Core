
import random
from datetime import datetime
import pytz
from dateutil.parser import parse as timeparse
import socket
from contextlib import closing

secure_random = random.SystemRandom()


# https://stackoverflow.com/questions/43270868/verify-if-a-local-port-is-available-in-python

def port_is_open(host, port):
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        if sock.connect_ex((host, port)) == 0:
            return True
        else:
            return False


def timestamp_to_local(epoch_ts, timezone):
    """Converts UNIX timestamp to local datetime object"""
    return datetime.fromtimestamp(epoch_ts, pytz.timezone(timezone))

def timestr_to_timestamp(time_string:str, timezone:str):
    timestamp = pytz.timezone(timezone).localize(timeparse(time_string))
    return int(timestamp.timestamp())

def process_profile(row, gen_scale=1, load_scale=1):
    """
        Converts a row of data from energy profile database to format usable by TREX

        Energy profile database is formatted based on a commonly found format by eGauge.
        Most notably, the following four columns exist in some form:
        tstamp, grid, solar, solar+
        Whereas tstamp is the UNIX timestamp, grid is the net load, solar and solar+ are the solar generation
        The only difference between solar and solar+ is that sometimes solar values can be slightly negative.
        The cause of this phenomenon is unknown, but the suspected cause may be due to reverse current flow in some
        parts of the measuring setup. Because of this, solar+ is used for calculations.

        Parameters
        ----------
        row : dict
            Must be a dict, or dict-compatible format consisting of at minimum tstamp, grid, and solar+
            tstamp should be a UNIX timestamp
            grid, solar, and solar+ should be in units of Wh
            the row indicates energy readings between the last tstamp and the end of the current tstamp
        gen_scale : float
            It is helpful to scale the raw profiles in for simulations.
            Default scale is 1
        load_scale : float
            It is helpful to scale the raw profiles in for simulations.
            Default scale is 1

        Returns
        -------
        generation:int, consumption:int
            returns generation and consumption as integer values in units of Wh
            The error accumulated is well within acceptable range
            Integer measures should also be compatible with pulse-watt readings from real meters,
            making transition to real system relatively easy and seamless
        """

    if row is not None:
        # if the data has been pre-processed
        if 'generation' in row and 'consumption' in row:
            consumption = int(round(load_scale * row['consumption'], 0))
            generation = int(round(gen_scale * row['generation'], 0))
        else:
            # else, calculate generation and consumption on the fly
            consumption = int(round(load_scale * (row['grid'] + row['solar+']), 0))
            generation = int(round(gen_scale * row['solar+'], 0))
        return generation, consumption
    return 0, 0

def energy_to_power(generation, consumption, duration=60, net_load=False):
    # convert from energy (Wh) to average power during interval (s) to kW
    duration_hour_fraction = duration / 3600
    gen_avg_kw = generation / duration_hour_fraction / 1000
    load_avg_kw = consumption / duration_hour_fraction / 1000

    if net_load:
        return load_avg_kw - gen_avg_kw
    else:
        return gen_avg_kw, load_avg_kw