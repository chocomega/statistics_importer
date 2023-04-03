# MyElectricalData Statistics Importer for Home Assistant

This Python script retrieves statistics from [MyElectricalData](https://github.com/m4dm4rtig4n/myelectricaldata/)'s cache database and imports them into Home Assistant via the [WebSocket API](https://developers.home-assistant.io/docs/api/websocket/).

Long Term Statistics will be created in Home Assistant and usable in the Energy Dashboard.

## Pre-requisites
- Python 3.4 or higher
- A running instance of Home Assistant 2022.10.0 or higher
- A running instance of MyElectricalData 0.8.13-11 or higher
- MyElectricalData must be configured with the cache and hourly details enabled, cf. [wiki](https://github.com/m4dm4rtig4n/myelectricaldata/wiki/03.-Configuration)
- A Long-lived access token created in Home Assistant

## Installation

1. Clone this repository or download the source code
2. The folder containing the source code should be preferrably located in Home Assistant's folder `config`
2. If you don't intend to execute the script from Home Assistant environment, install the required packages by running the following command:
```console
pip install -r requirements.txt
```

## Configuration

Rename the `script_config.example.yaml` file to `script_config.yaml` and edit it to suit your needs. All the following keys are mandatory:

- `ha_url`: URL of your Home Assistant instance
- `ha_use_ssl`: Specify if Home Assistant uses SSL or not
- `ha_access_token`: Long-lived access token for your Home Assistant instance
- `med_cache_db_path`: Path to the cache database of MyElectricalData
- `med_config_path`: Path to the configuration file of MyElectricalData

## Usage

```console
python statistics_importer.py [-h] [-d] [-f]
```

The script accepts the following options:

- `-h, --help`: show the help message and exit
- `-d, --delete-all`: delete all the statistics imported by this tool in Home Assistant, no import is done
- `-f, --force-all`: force the import of all statistics regardless of the last one already in Home Assistant

## Automation with Home Assistant
The script can be accessed as a service in Home Assistant with the [Shell Command integration](https://www.home-assistant.io/integrations/shell_command/).

Example of  `configuration.yaml` entry in Home Assistant, assuming the folder `statistics_importer` is located in the folder `config`:
```yaml
shell_command:
    statistics_import: python statistics_importer/statistics_importer.py
    statistics_delete_all: python statistics_importer/statistics_importer.py -d
```

You can then create an automation calling the service `shell_command.statistics_import` periodically or when MyElectricalData cache is updated.

## Warning
- Home Assistant may take some time to display the newly created statistics. Please wait, they will eventually show up in the UI.
- In case things go wrong you can still delete the long term statistics created by this script. To do so, it is advised to remove them from the Energy Dashboard first then execute the script with the option `-d`.

## License

This script is released under the [MIT License](https://opensource.org/licenses/MIT).
