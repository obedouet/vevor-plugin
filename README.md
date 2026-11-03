# Domoticz plugin for Vevor weather station

This plugin support Vevor Weather station with Domoticz. As the radio protocol is not supported, it is based on a reverse engineering of the Tuya protocol made by https://github.com/lasdou/vevor-yt60311

This plugin has also been inspired by https://github.com/kovariktomas/Domoticz-PWS-Plugin

## Installation

Create a python venv and pip install from requirements.txt
Copy this directory under plugins in your domoticz installation.
Start domoticz from the python venv.

## Usage

For the Domoticz configuration in Hardware tab, you need:

* IP address of the vevor weather station on your network
* Device ID: enter the value from the procedure in https://github.com/lasdou/vevor-yt60311 
* API key: enter the value from the procedure in https://github.com/lasdou/vevor-yt60311

Note: you can test values using https://github.com/lasdou/vevor-yt60311/blob/main/vevor_station.py

Automatically it will create the following devices:

* Outdoor Temperature
* Outdoor Humidity
* Indoor Temperature
* Indoor Humidity
* Wind
* Rain
* UV Index
* Pressure
* Battery Level

## Issues

* Rain does not report correctly.
* Tuya protocol seam to start a thread still not stopped correctly by the plugin when deactivating the hardware

## License

See LICENSE
