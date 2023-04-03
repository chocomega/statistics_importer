__version__ = '1.0.0'

import aiohttp
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math
import os
import pytz
import sqlite3
import sys
import time
import traceback
from typing import Any, Dict, Optional
import yaml

MED_CONFIG_DATE_FORMAT: str = "%Y-%m-%d"
MED_CACHE_DB_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
TZ_PARIS = pytz.timezone('Europe/Paris')

# Get the directory containing the script file
script_dir = os.path.dirname(os.path.abspath(__file__))

# Change the current working directory
os.chdir(script_dir)


@dataclass
class Config:
    ha_url: str
    ha_access_token: str
    med_cache_db_path: str
    med_config_path: str

    @classmethod
    def load(cls, path: str = os.path.abspath("script_config.yaml")) -> "Config":
        with open(path, "r") as f:
            config_dict = yaml.safe_load(f)
        return cls(**config_dict)


class Unit(Enum):
    KILO_WATT_HOUR = "kWh"
    EURO = "EUR"


class ElectricityType(Enum):
    CONSUMPTION = "consumption"
    PRODUCTION = "production"


class TariffType(Enum):
    BASE = "base"
    HC = "hc"
    HP = "hp"


class PlanType(Enum):
    BASE = "BASE"
    HCHP = "HC/HP"


TariffsPrices = Dict[TariffType, float]
PlanPrices = Dict[ElectricityType, TariffsPrices]


class Plan:
    def __init__(self, name: str, prices: PlanPrices) -> None:
        self.name = name
        self._prices = prices
        self.tariff_types = {
            electricity_type: [tariff_type for tariff_type in tariffs_prices.keys()] for electricity_type, tariffs_prices in prices.items()
        }

    def get_price(self, electricty_type: ElectricityType, tariff_type: TariffType, date: Optional[datetime] = None) -> float:
        return self._prices[electricty_type][tariff_type]


class PlanBase(Plan):
    def __init__(self, consumption_price: float, production_price: float) -> None:
        super().__init__("BASE", {
            ElectricityType.CONSUMPTION: {
                TariffType.BASE: consumption_price
            },
            ElectricityType.PRODUCTION: {
                TariffType.BASE: production_price
            }
        })


class PlanHCHP(Plan):
    def __init__(self, HC_consumption_price: float, HP_consumption_price: float, production_price: float) -> None:
        super().__init__("HC/HP", {
            ElectricityType.CONSUMPTION: {
                TariffType.HC: HC_consumption_price,
                TariffType.HP: HP_consumption_price,
            },
            ElectricityType.PRODUCTION: {
                TariffType.BASE: production_price
            }
        })


StatisticData = Dict[str, Any]


class StatisticMetadata:
    def __init__(self, usage_point_id: str, electricity_type: ElectricityType, tariff_type: TariffType, unit_of_measurement: Unit, max_date: datetime) -> None:
        # Metadata for MyElectricalData
        self.usage_point_id = usage_point_id
        self.max_date = max_date
        self.electricity_type = electricity_type
        self.tariff_type = tariff_type
        self.db_table_id = f"{electricity_type.value}_detail"
        # Metadata for Home Assistant
        self.unit_of_measurement = unit_of_measurement
        self.source = "myelectricaldata"
        is_cost = (unit_of_measurement == Unit.EURO)
        # id = myelectricaldata:xxx_(base|hc|hp)_(consumption|production)_(cost)
        self.id = f"{self.source}:{usage_point_id}_{tariff_type.value}_{electricity_type.value}{'_cost' if is_cost else ''}"
        # TODO use name in config.yaml ?
        self.name = f"MyElectricalData - {usage_point_id} {tariff_type.name} {electricity_type.value}{' cost' if is_cost else ''}"


def to_float(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return math.nan


def get_max_date_from_med_config(usage_point_config: dict[str, str], statistics_key: str) -> datetime:
    try:
        max_date_str = usage_point_config[f"{statistics_key}_max_date"]
        max_date = datetime.strptime(max_date_str, MED_CONFIG_DATE_FORMAT)
    except ValueError:
        max_date = datetime.fromtimestamp(0)

    max_date = TZ_PARIS.localize(max_date)
    return max_date


def create_plan_from_med_config(usage_point_config: dict[str, str]):
    plan_type = PlanType(usage_point_config["plan"])
    if plan_type == PlanType.BASE:
        plan = PlanBase(to_float(usage_point_config["consumption_price_base"]),
                        to_float(usage_point_config["production_price"]))
    elif plan_type == PlanType.HCHP:
        plan = PlanHCHP(to_float(usage_point_config["consumption_price_hc"]),
                        to_float(usage_point_config["consumption_price_hp"]),
                        to_float(usage_point_config["production_price"]))
    else:
        raise Exception("  Invalid Plan:", plan_type)
    return plan


def export_statistics_from_db(db_cursor: sqlite3.Cursor, stat_metadata: StatisticMetadata, start_date: datetime, sum_offset: float, plan: Plan) -> list[StatisticData]:
    is_cost = (stat_metadata.unit_of_measurement == Unit.EURO)
    is_base_tariff = (stat_metadata.tariff_type == TariffType.BASE)
    # Select the sum of the value column aggregated by hour
    # The sum is divided by 2 to convert from 'kW for 30 min' to 'kW for 1 hour' (i.e. kWh)
    query = f'SELECT strftime("%Y-%m-%d %H:00:00", date) as hour, SUM(value)/2. as total ' \
            f'FROM {stat_metadata.db_table_id} ' \
            f'WHERE date >= ? AND usage_point_id = ? {"" if is_base_tariff else "AND measure_type = ? "}' \
            f'GROUP BY hour'

    if is_base_tariff:
        paramaters = (start_date, stat_metadata.usage_point_id)
    else:
        paramaters = (start_date, stat_metadata.usage_point_id,
                      stat_metadata.tariff_type.name)

    db_cursor.execute(query, paramaters)
    rows = db_cursor.fetchall()

    stats = []
    # Offset the sum by sum_offset for continuity with the previous stats
    # sum is multiplied by 1000 in order to avoid float precision issue
    # then divided back by 1000
    sum = sum_offset * 1000
    for row in rows:
        localized_start_date = TZ_PARIS.localize(
            datetime.strptime(row[0], MED_CACHE_DB_DATE_FORMAT))
        value = row[1]
        sum += value * plan.get_price(stat_metadata.electricity_type,
                                      stat_metadata.tariff_type, localized_start_date) if is_cost else value
        stats.append({
            "start": localized_start_date.isoformat(),
            "state": value/1000.,
            "sum": sum/1000.
        })

    # print(stats[0])
    # print(stats[-1])
    return stats


class HomeAssistantWebSocketHelper:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self._websocket = websocket
        self._command_id = 0

    async def authenticate(self, access_token: str) -> None:
        response = await self._websocket.receive_json()
        print(f"authenticate: received response {response}")
        if response["type"] != "auth_required":
            raise Exception(
                f"authenticate: invalid server response {response}")

        # Auth
        await self._websocket.send_json({
            "type": "auth",
            "access_token": access_token
        })

        response = await self._websocket.receive_json()
        print(f"authenticate: received response {response}")
        if response["type"] != "auth_ok":
            raise Exception(
                f"authenticate: auth NOT ok, check Home Assistant Long-Lived Access Token")

    async def recorder_import_statistics(self, stat_metadata: StatisticMetadata, stats: list[StatisticData]) -> None:
        self._command_id += 1
        await self._websocket.send_json({
            "id": self._command_id,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": stat_metadata.name,
                "source": stat_metadata.source,
                "statistic_id": stat_metadata.id,
                "unit_of_measurement": stat_metadata.unit_of_measurement.value,
            },
            "stats": stats
        })

        response = await self._websocket.receive_json()
        print(f"recorder_import_statistics: received response {response}")
        if not response["success"]:
            raise Exception(f"recorder_import_statistics: failed")

    async def recorder_list_statistic_ids(self) -> list[dict]:
        self._command_id += 1
        response = await self._websocket.send_json({
            "id":  self._command_id,
            "type": "recorder/list_statistic_ids",
            "statistic_type": "sum",
        })

        response = await self._websocket.receive_json()
        # print(f"recorder_list_statistic_ids: received response {response}")

        if response["type"] != "result" or not response["success"]:
            raise Exception(f"recorder_list_statistic_ids: failed")

        return response["result"]

    async def recorder_clear_statistics(self, statistic_ids: list[str]) -> None:
        self._command_id += 1
        response = await self._websocket.send_json({
            "id":  self._command_id,
            "type": "recorder/clear_statistics",
            "statistic_ids": statistic_ids,
        })

        response = await self._websocket.receive_json()
        print(f"recorder_clear_statistics: received response {response}")
        if not response["success"]:
            raise Exception(f"recorder_clear_statistics: failed")

    async def recorder_statistics_during_period(self, stat_metadata: StatisticMetadata, start_time: datetime, end_time: datetime) -> dict[str, list[StatisticData]]:
        self._command_id += 1
        await self._websocket.send_json({
            "id":  self._command_id,
            "type": "recorder/statistics_during_period",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "statistic_ids": [stat_metadata.id],
            "period": "hour",
        })

        response = await self._websocket.receive_json()
        # print(f"recorder_statistics_during_period: received response {response}")

        if response["type"] != "result" or not response["success"]:
            raise Exception(f"recorder_statistics_during_period: failed")

        return response["result"]

    async def recorder_purge(self) -> None:
        self._command_id += 1
        await self._websocket.send_json({
            "id":  self._command_id,
            "type": "call_service",
            "domain": "recorder",
            "service": "purge",
            "service_data": {
                "repack": "false",
                "apply_filter": "false"
            }
        })

        response = await self._websocket.receive_json()
        print(f"recorder_purge: received response {response}")
        if not response["success"]:
            raise Exception(f"recorder_purge: failed")

    async def get_last_statistic(self, stat_metadata: StatisticMetadata, days_before_now: int) -> StatisticData:
        end_time = datetime.now(TZ_PARIS)
        start_time = end_time - timedelta(days=days_before_now)
        result = await self.recorder_statistics_during_period(stat_metadata, start_time, end_time)
        if not result:
            # No previous stats found
            return {}
        else:
            # Return last stat
            return result[stat_metadata.id][-1]

    async def import_statistics(self, db_cursor: sqlite3.Cursor, stat_metadata: StatisticMetadata, plan: Plan, force_import_all: bool, days_before_now: int = 10):
        start_date = stat_metadata.max_date
        sum_offset = 0

        if force_import_all:
            print("    Force import all statistics")
        else:
            print(
                f"    Getting the last statistic data point from Home Assistant, up to {days_before_now} days back from now")
            last_stat = await self.get_last_statistic(stat_metadata, days_before_now)

            if last_stat:
                print("    Previous statistic data point found")
                # 'start' timestamp in HA is in ms
                start_date = datetime.fromtimestamp(
                    last_stat["start"]/1000) + timedelta(hours=1)
                sum_offset = last_stat["sum"]
            else:
                print("    No previous statistic data point found")

        print(
            f"    Exporting statistics from cache since {start_date}, with a sum offset of {sum_offset:.2f} {stat_metadata.unit_of_measurement.value}")
        stats = export_statistics_from_db(
            db_cursor, stat_metadata, start_date, sum_offset, plan)

        if stats:
            print(
                f"    Importing {len(stats)} statistic data points into Home Assistant")
            await self.recorder_import_statistics(stat_metadata, stats)
        else:
            print(f"    No statistics found from cache to import into Home Assistant")

    async def import_statistics_from_med(self, med_cache_db_path: str, med_config: dict, force_import_all: bool):
        # Check cache.db exists
        if not os.path.exists(med_cache_db_path):
            raise FileNotFoundError(
                f"{med_cache_db_path} not found")

        # Connect to cache.db
        db_connection = sqlite3.connect(med_cache_db_path)
        db_cursor = db_connection.cursor()

        try:
            # TODO build list of StatMetadata from config first, then loop on list ?
            for usage_point_id in med_config["myelectricaldata"]:
                print("#", usage_point_id)
                usage_point_config = med_config["myelectricaldata"][usage_point_id]

                plan = create_plan_from_med_config(usage_point_config)

                for electricity_type in ElectricityType:
                    statistics_detail_key = f"{electricity_type.value}_detail"
                    if usage_point_config[statistics_detail_key] != 'true':
                        print(
                            f"  {statistics_detail_key} not enabled, skipping")
                        break

                    max_date = get_max_date_from_med_config(
                        usage_point_config, statistics_detail_key)

                    for tariff_type in plan.tariff_types[electricity_type]:
                        print(" ", tariff_type.name,
                              "ENERGY", electricity_type.name)
                        stat_metadata = StatisticMetadata(
                            usage_point_id, electricity_type, tariff_type, Unit.KILO_WATT_HOUR, max_date)
                        await self.import_statistics(db_cursor, stat_metadata, plan, force_import_all)

                        print(" ", tariff_type.name,
                              electricity_type.name, "COST")
                        tariff_price = plan.get_price(
                            electricity_type, tariff_type)
                        if (math.isnan(tariff_price)):
                            print(
                                f"    Tariff's price is not a number, skipping cost statistics export")
                        else:
                            print(f"    Price: {tariff_price} EUR/kWh")
                            stat_metadata = StatisticMetadata(
                                usage_point_id, electricity_type, tariff_type, Unit.EURO, max_date)
                            await self.import_statistics(db_cursor, stat_metadata, plan, force_import_all)

        finally:
            db_cursor.close()
            db_connection.close()

    async def delete_all_med_statistics(self):
        result = await self.recorder_list_statistic_ids()
        filtered_list = [x['statistic_id']
                         for x in result if x['source'] == 'myelectricaldata']

        if filtered_list:
            print("Deleting the following statistics:")
            for statistic_id in filtered_list:
                print(statistic_id)

            await self.recorder_clear_statistics(filtered_list)
        else:
            print("No stats to delete")


async def main(args: argparse.Namespace) -> int:
    print(datetime.now(TZ_PARIS).strftime('-- %a %d-%m-%Y %H:%M:%S --'))
    start_time = time.time()

    try:
        # Read config.yaml
        config = Config.load()

        # Read MyElectricalData config.yaml
        with open(os.path.abspath(config.med_config_path)) as file:
            med_config = yaml.safe_load(file)

        # Create the WebSocket connection
        url = f"ws://{config.ha_url}/api/websocket"
        print("Connecting to websocket at", url)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as websocket:
                ha_ws = HomeAssistantWebSocketHelper(websocket)

                # Must authenticate before sending commands
                print("Authenticating with Home Assistant")
                await ha_ws.authenticate(config.ha_access_token)

                if args.delete_all:
                    # Warning ! User must remove the stats from the Energy Dashboard first
                    await ha_ws.delete_all_med_statistics()

                else:
                    await ha_ws.import_statistics_from_med(config.med_cache_db_path, med_config, args.force_all)

    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)
        print(f"ERROR on line {tb[0].lineno}: {tb[0].line}")
        print(f"{type(e).__name__} - {e}")
        return 1

    finally:
        print(f"Elapsed time: {time.time() - start_time:.2f} seconds")

    return 0

parser = argparse.ArgumentParser(
    description="Export statistics from MyElectricalData's cache and import them into Home Assistant")
parser.add_argument('-d', '--delete-all', action='store_true',
                    help='delete all the statistics imported by this tool in Home Assistant, no import is done')
parser.add_argument('-f', '--force-all', action='store_true',
                    help='force the import of all statistics regardless of the last one already in Home Assistant')
args = parser.parse_args()

if args.delete_all and args.force_all:
    parser.error("--force-all can only be used for import")

sys.exit(asyncio.run(main(args)))

# TODO logging
