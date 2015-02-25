## Resol VBus LAN adapter graphing tool

This python script parses a tcp stream as obtained from the Resol VBUS LAN adapter and writes it to a round robin database (rrd). Periodically, graphs are created from the rrd for display by e.g. a http server.

The software has been tested for the DeltaSol ES controller, but could work for other controllers as well.

## Installation

1. make an install directory, e.g. /usr/local/resol
2. cp resol.py and resol_daemon.cfg into /usr/local/resol
3. copy (as root) the ubuntu startup scrip to /etc/init:
   cp start_scripts/ubuntu/resol.cfg /etc/init/

## Configuration

1. edit resol_daemon.cfg
  Adjust the following according to the installation directory you picked earlier:

    home_directory=/usr/local/resol

  Set the hostname under which the LAN adapter is available on the net:
    [vbus_adapter]
    hostname=hostname.of.resol.adapter
    port=7053

  Pick a name for the rrd database file. Make sure this directory does not get wiped out on machine reboot:
    rrd_filename=/var/tmp/resol.rrd

  Decide where you want the generated graphs to be written:
    graph_output_dir=/var/www/html/resol

  Adjust the graphs as you see fit. See default config file for examples:

    [preheat_pump_week]
    timespan=WEEK
    title=preheat pump activity last 7 days
    plot=PUMP5:AVERAGE:preheat:FF0000:1.0:
    units=%

2. edit the Ubuntu startup script /etc/resol.cfg and adjust it to match your installation directory

## Running

On Ubuntu:

    service resol start

On other OSes, please write your own start/stop script and contribute it to the project.
