# Copyright (c) 2017 - Internet Association of Australia

# Description:
# -----------------------------------------------------------------------------
# This is a basic script designed to run on-box inside Extreme Networks equipment
# to pull optic metrics and push them to an InfluxDB endpoint. 
#

# Deployment:
# -----------------------------------------------------------------------------
# The best advice is to use UPM to schedule this script to run at a given interval.
#
#   create upm profile optic_metrics_to_influxdb
#   create upm timer timer_1m
#   configure upm timer timer_1m profile optic_metrics_to_influxdb
#   configure upm timer timer_1m after 10 every 60
#
# Where the profile uses the following command:
#
#   ************Profile Contents Begin************
#        run script extreme_optics_influxdb.py <device_name> <influxdb_host> <influxdb_username> <influxdb_password> <influxdb_database>
#   ************Profile Contents Ends*************
#

import xml.etree.ElementTree as et
import exsh
import re, urllib2, base64, argparse, socket, httplib

METRICS = {
        'txPower'       : 'tx-power',
        'rxPower'       : 'rx-power',
        'txBiasCurrent' : 'tx-current'
}

# Extreme Networks ioctl to change the VR that a socket operates in.
EXTREME_SO_VRID = 37 

# VR ID of the VR-Defaut virtual router.
EXTREME_VR_DEFAULT_ID = 2

# Fetch Optic stats from the device in XML and parse it into a nice datastructure
def get_optics_data():
    # issue command to get the XML data for all optics.
    try:
        xml_reply = exsh.clicmd("show ports transceiver information", xml=True)
    except RuntimeError:
        exsh.clicmd('Command "show ports transceiver information" is invalid')
        return

    # check we actually got a valid response
    if xml_reply is None:
        exsh.clicmd('create log message "Unable to get optics data with command"')
        return

    # parse the data into a nicer, data structure
    # multiple XML trees are returned as one string, need to split them.
    ports_raw = xml_reply.replace("</reply><reply>", "</reply>\n<reply>").split('\n')

    # parse the data into a nicer, data structure
    # multiple XML trees are returned as one string, need to split them.
    ports_data = []

    ports_parsed = [et.fromstring(x).findall(
        '.message/show_ports_transceiver')[0] for x in ports_raw]

    ports_parsed_iter = iter(ports_parsed)

    for port in ports_parsed_iter:
        data = {}
        # check there isn't a portErrorString element, if not we can assume there is an optic
        if len(port) > 0 and len(port.findall('portErrorString')) == 0 and port.findall('partNumberIsValid')[0].text == '1':
            data['name'] = port.findall('port')[0].text
            # make the port_name match the ifName - unstacked switches should still return a 1:xx port number.
            if not re.match('^\d+:\d+$', data['name']):
                # dodgy, I know but it's how extreme do it
                data['name'] = "1:" + data['name']

            data['channels'] = {}
            num_channels = int(port.findall('numChannels')[0].text)
            curr_chan = port

            for chan in range(0, num_channels):
                data['channels'][chan] = {}
                # Temp and voltage is only present on the top level channel
                if chan == 0:
                    data['temperature'] = curr_chan.findall('temp')[0].text
                    data['voltage'] = curr_chan.findall('voltageAux1')[0].text
                for metric in METRICS.keys():
                    # make prettier key names with consistent formatting
                    data['channels'][chan][METRICS[metric]] = curr_chan.findall(metric)[0].text
                if chan+1 != num_channels:
                    curr_chan = ports_parsed_iter.next()  # go to the next port

            ports_data.append(data)

    return ports_data



def fix_extreme_inf_values(data):
    # Extremes that show optics with -Inf RX power sometimes returns -9999.000000.
    # Lets fix that to be the same as other ports with no rx detected.
    for port in data:
        for channel in port['channels'].values():
            if channel['rx-power'] == '-9999.000000':
                channel['rx-power'] = '-40.000000'
            if channel['tx-power'] == '-9999.000000':
                channel['tx-power'] = '0.00'
    return data


# Return the collected data in the InfluxDB Line Protocol Format
# Ref: https://docs.influxdata.com/influxdb/v1.3/write_protocols/line_protocol_tutorial/#syntax
def create_lineprotocol_data(ports_data, device_name):
    line_data = []

    for port in ports_data:
        measurement = "optics"
        tags = "device=" + device_name + ",port=" + port['name']
        fields = ",".join([key + "=" + port[key] for key in port.keys() if key not in  ['name', 'channels']])
        line_data.append(measurement + "," + tags + " " + fields)

        for channel, chan_data in port['channels'].items():
            channel_measurement = "optics_channels"
            channel_tags = ",".join([tags, "channel=" + str(channel)])
            channel_fields = ",".join(
                [key + "=" + chan_data[key] for key in chan_data.keys()]
            )
            line_data.append(channel_measurement + "," +
                             channel_tags + " " + channel_fields)

    return "\n".join(line_data)

# HTTP/S POST our data to InfluxDB
# Ref: https://docs.influxdata.com/influxdb/v1.3/guides/writing_data/#writing-data-using-the-http-api
def post_influx_data(influxdb_host, influxdb_port, target_vr, ssl_enabled, influxdb_username, influxdb_password, influxdb_database, data):
    # Patching the connect() method to add the required Extreme Socket options for the non-default VR
    def monkey_connect(self):
        #orig_connect(self)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, EXTREME_SO_VRID, target_vr)
        self.sock.connect((self.host,self.port))

        # pulled for the original source, leaving here just for reference
        # Ref: https://github.com/python/cpython/blob/2.7/Lib/httplib.py
        if self._tunnel_host:
            self._tunnel()
    
    # swap in our patched method for the original one
    httplib.HTTPConnection.connect = monkey_connect

    conn = None
    
    try:
        if ssl_enabled:
            # HTTPSConnection calls HTTPConnection.connect() thus allowing the above patch to work for HTTPS too.
            conn = httplib.HTTPSConnection('%(influxdb_host)s:%(influxdb_port)s' % locals())
        else:
            conn = httplib.HTTPConnection('%(influxdb_host)s:%(influxdb_port)s' % locals())


        headers = { "Content-Type" : "application/octet-stream", "Content-Length" : str('%d' % len(data)), 'Authorization' : 'Basic %s' % base64.b64encode(influxdb_username + ":" + influxdb_password)}
        conn.request ('POST', '/write?db=%(influxdb_database)s' % locals(), data, headers)

    except Exception, e:
        exsh.clicmd('create log message "Unable to create connection to ' + influxdb_host + ':' + influxdb_port + ' - ' + repr(e) + '"')
        return
    
    response = conn.getresponse()

    if response.status != 204:
        exsh.clicmd('create log message "Unable to POST data to ' + influxdb_host + ':' + influxdb_port + '"')
        return


#####
#
# Main
#

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send SFP Optic metrics to InfluxDB')
    parser.add_argument('device_name', metavar="device", help="Device name to be used as an InfluxDB Tag.")
    parser.add_argument('influxdb_host', metavar="host", help="Hostname to connect to InfluxDB with.")
    parser.add_argument('influxdb_username', metavar="username", help="InfluxDB Username with write access to the database.")
    parser.add_argument('influxdb_password', metavar="password", help="InfluxDB Password.")
    parser.add_argument('influxdb_database', metavar="database", help="InfluxDB database name to write metrics to.")
    parser.add_argument('--influxdb_port', metavar="port", help="Port to connect to InfluxDB on, defaults to 8086.", default="8086")
    parser.add_argument('--ssl', dest='ssl_enabled', action='store_true', help="Connect to InfluxDB over an HTTPS connection, defaults to no.")
    parser.add_argument('--vr', dest='target_vr', type=int, help="The ID of the Virtual Router to use. Defaults to '2' or the Default-VR.", default=EXTREME_VR_DEFAULT_ID)
    args = parser.parse_args()
    
    # grab data
    data = get_optics_data()

    if data:
        fixed_data = fix_extreme_inf_values(data)
        line_data = create_lineprotocol_data(fixed_data, args.device_name)
        post_influx_data(args.influxdb_host, args.influxdb_port, args.target_vr, args.ssl_enabled, args.influxdb_username, args.influxdb_password, args.influxdb_database, line_data)
