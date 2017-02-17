#   Copyright (C) 2013-2014 Canonical Ltd.
#
#   Author: Scott Moser <scott.moser@canonical.com>
#   Author: Blake Rouse <blake.rouse@canonical.com>
#
#   Curtin is free software: you can redistribute it and/or modify it under
#   the terms of the GNU Affero General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#
#   Curtin is distributed in the hope that it will be useful, but WITHOUT ANY
#   WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
#   FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for
#   more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with Curtin.  If not, see <http://www.gnu.org/licenses/>.

import errno
import glob
import os
import re

from curtin.log import LOG
from curtin.udev import generate_udev_rule
import curtin.util as util
import curtin.config as config
from . import network_state

SYS_CLASS_NET = "/sys/class/net/"

NET_CONFIG_OPTIONS = [
    "address", "netmask", "broadcast", "network", "metric", "gateway",
    "pointtopoint", "media", "mtu", "hostname", "leasehours", "leasetime",
    "vendor", "client", "bootfile", "server", "hwaddr", "provider", "frame",
    "netnum", "endpoint", "local", "ttl",
    ]

NET_CONFIG_COMMANDS = [
    "pre-up", "up", "post-up", "down", "pre-down", "post-down",
    ]

NET_CONFIG_BRIDGE_OPTIONS = [
    "bridge_ageing", "bridge_bridgeprio", "bridge_fd", "bridge_gcinit",
    "bridge_hello", "bridge_maxage", "bridge_maxwait", "bridge_stp",
    ]


def sys_dev_path(devname, path=""):
    return SYS_CLASS_NET + devname + "/" + path


def read_sys_net(devname, path, translate=None, enoent=None, keyerror=None):
    try:
        contents = ""
        with open(sys_dev_path(devname, path), "r") as fp:
            contents = fp.read().strip()
        if translate is None:
            return contents

        try:
            return translate.get(contents)
        except KeyError:
            LOG.debug("found unexpected value '%s' in '%s/%s'", contents,
                      devname, path)
            if keyerror is not None:
                return keyerror
            raise
    except OSError as e:
        if e.errno == errno.ENOENT and enoent is not None:
            return enoent
        raise


def is_up(devname):
    # The linux kernel says to consider devices in 'unknown'
    # operstate as up for the purposes of network configuration. See
    # Documentation/networking/operstates.txt in the kernel source.
    translate = {'up': True, 'unknown': True, 'down': False}
    return read_sys_net(devname, "operstate", enoent=False, keyerror=False,
                        translate=translate)


def is_wireless(devname):
    return os.path.exists(sys_dev_path(devname, "wireless"))


def is_connected(devname):
    # is_connected isn't really as simple as that.  2 is
    # 'physically connected'. 3 is 'not connected'. but a wlan interface will
    # always show 3.
    try:
        iflink = read_sys_net(devname, "iflink", enoent=False)
        if iflink == "2":
            return True
        if not is_wireless(devname):
            return False
        LOG.debug("'%s' is wireless, basing 'connected' on carrier", devname)

        return read_sys_net(devname, "carrier", enoent=False, keyerror=False,
                            translate={'0': False, '1': True})

    except IOError as e:
        if e.errno == errno.EINVAL:
            return False
        raise


def is_physical(devname):
    return os.path.exists(sys_dev_path(devname, "device"))


def is_present(devname):
    return os.path.exists(sys_dev_path(devname))


def get_devicelist():
    return os.listdir(SYS_CLASS_NET)


class ParserError(Exception):
    """Raised when parser has issue parsing the interfaces file."""


def parse_deb_config_data(ifaces, contents, src_dir, src_path):
    """Parses the file contents, placing result into ifaces.

    '_source_path' is added to every dictionary entry to define which file
    the configration information came from.

    :param ifaces: interface dictionary
    :param contents: contents of interfaces file
    :param src_dir: directory interfaces file was located
    :param src_path: file path the `contents` was read
    """
    currif = None
    for line in contents.splitlines():
        line = line.strip()
        if line.startswith('#'):
            continue
        split = line.split(' ')
        option = split[0]
        if option == "source-directory":
            parsed_src_dir = split[1]
            if not parsed_src_dir.startswith("/"):
                parsed_src_dir = os.path.join(src_dir, parsed_src_dir)
            for expanded_path in glob.glob(parsed_src_dir):
                dir_contents = os.listdir(expanded_path)
                dir_contents = [
                    os.path.join(expanded_path, path)
                    for path in dir_contents
                    if (os.path.isfile(os.path.join(expanded_path, path)) and
                        re.match("^[a-zA-Z0-9_-]+$", path) is not None)
                ]
                for entry in dir_contents:
                    with open(entry, "r") as fp:
                        src_data = fp.read().strip()
                    abs_entry = os.path.abspath(entry)
                    parse_deb_config_data(
                        ifaces, src_data,
                        os.path.dirname(abs_entry), abs_entry)
        elif option == "source":
            new_src_path = split[1]
            if not new_src_path.startswith("/"):
                new_src_path = os.path.join(src_dir, new_src_path)
            for expanded_path in glob.glob(new_src_path):
                with open(expanded_path, "r") as fp:
                    src_data = fp.read().strip()
                abs_path = os.path.abspath(expanded_path)
                parse_deb_config_data(
                    ifaces, src_data,
                    os.path.dirname(abs_path), abs_path)
        elif option == "auto":
            for iface in split[1:]:
                if iface not in ifaces:
                    ifaces[iface] = {
                        # Include the source path this interface was found in.
                        "_source_path": src_path
                    }
                ifaces[iface]['auto'] = True
                ifaces[iface]['control'] = 'auto'
        elif option.startswith('allow-'):
            for iface in split[1:]:
                if iface not in ifaces:
                    ifaces[iface] = {
                        # Include the source path this interface was found in.
                        "_source_path": src_path
                    }
                ifaces[iface]['auto'] = False
                ifaces[iface]['control'] = option.split('allow-')[-1]
        elif option == "iface":
            iface, family, method = split[1:4]
            if iface not in ifaces:
                ifaces[iface] = {
                    # Include the source path this interface was found in.
                    "_source_path": src_path
                }
            # man (5) interfaces says we can have multiple iface stanzas
            # all options are combined
            ifaces[iface]['family'] = family
            ifaces[iface]['method'] = method
            currif = iface
        elif option == "hwaddress":
            ifaces[currif]['hwaddress'] = split[1]
        elif option in NET_CONFIG_OPTIONS:
            ifaces[currif][option] = split[1]
        elif option in NET_CONFIG_COMMANDS:
            if option not in ifaces[currif]:
                ifaces[currif][option] = []
            ifaces[currif][option].append(' '.join(split[1:]))
        elif option.startswith('dns-'):
            if 'dns' not in ifaces[currif]:
                ifaces[currif]['dns'] = {}
            if option == 'dns-search':
                ifaces[currif]['dns']['search'] = []
                for domain in split[1:]:
                    ifaces[currif]['dns']['search'].append(domain)
            elif option == 'dns-nameservers':
                ifaces[currif]['dns']['nameservers'] = []
                for server in split[1:]:
                    ifaces[currif]['dns']['nameservers'].append(server)
        elif option.startswith('bridge_'):
            if 'bridge' not in ifaces[currif]:
                ifaces[currif]['bridge'] = {}
            if option in NET_CONFIG_BRIDGE_OPTIONS:
                bridge_option = option.replace('bridge_', '', 1)
                ifaces[currif]['bridge'][bridge_option] = split[1]
            elif option == "bridge_ports":
                ifaces[currif]['bridge']['ports'] = []
                for iface in split[1:]:
                    ifaces[currif]['bridge']['ports'].append(iface)
            elif option == "bridge_hw" and split[1].lower() == "mac":
                ifaces[currif]['bridge']['mac'] = split[2]
            elif option == "bridge_pathcost":
                if 'pathcost' not in ifaces[currif]['bridge']:
                    ifaces[currif]['bridge']['pathcost'] = {}
                ifaces[currif]['bridge']['pathcost'][split[1]] = split[2]
            elif option == "bridge_portprio":
                if 'portprio' not in ifaces[currif]['bridge']:
                    ifaces[currif]['bridge']['portprio'] = {}
                ifaces[currif]['bridge']['portprio'][split[1]] = split[2]
        elif option.startswith('bond-'):
            if 'bond' not in ifaces[currif]:
                ifaces[currif]['bond'] = {}
            bond_option = option.replace('bond-', '', 1)
            ifaces[currif]['bond'][bond_option] = split[1]
    for iface in ifaces.keys():
        if 'auto' not in ifaces[iface]:
            ifaces[iface]['auto'] = False


def parse_deb_config(path):
    """Parses a debian network configuration file."""
    ifaces = {}
    with open(path, "r") as fp:
        contents = fp.read().strip()
    abs_path = os.path.abspath(path)
    parse_deb_config_data(
        ifaces, contents,
        os.path.dirname(abs_path), abs_path)
    return ifaces


def parse_net_config_data(net_config):
    """Parses the config, returns NetworkState dictionary

    :param net_config: curtin network config dict
    """
    state = None
    if 'version' in net_config and 'config' in net_config:
        ns = network_state.NetworkState(version=net_config.get('version'),
                                        config=net_config.get('config'))
        ns.parse_config()
        state = ns.network_state

    return state


def parse_net_config(path):
    """Parses a curtin network configuration file and
       return network state"""
    ns = None
    net_config = config.load_config(path)
    if 'network' in net_config:
        ns = parse_net_config_data(net_config.get('network'))

    return ns


def render_persistent_net(network_state):
    ''' Given state, emit udev rules to map
        mac to ifname
    '''
    content = "# Autogenerated by curtin\n"
    interfaces = network_state.get('interfaces')
    for iface in interfaces.values():
        if iface['type'] == 'physical':
            ifname = iface.get('name', None)
            mac = iface.get('mac_address', '')
            # len(macaddr) == 2 * 6 + 5 == 17
            if ifname and mac and len(mac) == 17:
                content += generate_udev_rule(ifname, mac.lower())

    return content


# TODO: switch valid_map based on mode inet/inet6
def iface_add_subnet(iface, subnet):
    content = ""
    valid_map = [
        'address',
        'netmask',
        'broadcast',
        'metric',
        'gateway',
        'pointopoint',
        'mtu',
        'scope',
        'dns_search',
        'dns_nameservers',
    ]
    for key, value in subnet.items():
        if value and key in valid_map:
            if type(value) == list:
                value = " ".join(value)
            if '_' in key:
                key = key.replace('_', '-')
            content += "    {} {}\n".format(key, value)

    return content


# TODO: switch to valid_map for attrs
def iface_add_attrs(iface, index):
    # If the index is non-zero, this is an alias interface. Alias interfaces
    # represent additional interface addresses, and should not have additional
    # attributes. (extra attributes here are almost always either incorrect,
    # or are applied to the parent interface.) So if this is an alias, stop
    # right here.
    if index != 0:
        return ""
    content = ""
    ignore_map = [
        'control',
        'index',
        'inet',
        'mode',
        'name',
        'subnets',
        'type',
    ]
    if iface['type'] not in ['bond', 'bridge', 'vlan']:
        ignore_map.append('mac_address')

    for key, value in iface.items():
        if value and key not in ignore_map:
            if type(value) == list:
                value = " ".join(value)
            content += "    {} {}\n".format(key, value)

    return content


def render_route(route, indent=""):
    """When rendering routes for an iface, in some cases applying a route
    may result in the route command returning non-zero which produces
    some confusing output for users manually using ifup/ifdown[1].  To
    that end, we will optionally include an '|| true' postfix to each
    route line allowing users to work with ifup/ifdown without using
    --force option.

    We may at somepoint not want to emit this additional postfix, and
    add a 'strict' flag to this function.  When called with strict=True,
    then we will not append the postfix.

    1. http://askubuntu.com/questions/168033/
             how-to-set-static-routes-in-ubuntu-server
    """
    content = []
    up = indent + "post-up route add"
    down = indent + "pre-down route del"
    or_true = " || true"
    mapping = {
        'network': '-net',
        'netmask': 'netmask',
        'gateway': 'gw',
        'metric': 'metric',
    }
    if route['network'] == '0.0.0.0' and route['netmask'] == '0.0.0.0':
        default_gw = " default gw %s" % route['gateway']
        content.append(up + default_gw + or_true)
        content.append(down + default_gw + or_true)
    elif route['network'] == '::' and route['netmask'] == 0:
        # ipv6!
        default_gw = " -A inet6 default gw %s" % route['gateway']
        content.append(up + default_gw + or_true)
        content.append(down + default_gw + or_true)
    else:
        route_line = ""
        for k in ['network', 'netmask', 'gateway', 'metric']:
            if k in route:
                route_line += " %s %s" % (mapping[k], route[k])
        content.append(up + route_line + or_true)
        content.append(down + route_line + or_true)
    return "\n".join(content)


def iface_start_entry(iface):
    fullname = iface['name']

    control = iface['control']
    if control == "auto":
        cverb = "auto"
    elif control in ("hotplug",):
        cverb = "allow-" + control
    else:
        cverb = "# control-" + control

    subst = iface.copy()
    subst.update({'fullname': fullname, 'cverb': cverb})

    return ("{cverb} {fullname}\n"
            "iface {fullname} {inet} {mode}\n").format(**subst)


def subnet_is_ipv6(subnet):
    # 'static6' or 'dhcp6'
    if subnet['type'].endswith('6'):
        # This is a request for DHCPv6.
        return True
    elif subnet['type'] == 'static' and ":" in subnet['address']:
        return True
    return False


def render_interfaces(network_state):
    ''' Given state, emit etc/network/interfaces content '''

    content = ""
    interfaces = network_state.get('interfaces')
    ''' Apply a sort order to ensure that we write out
        the physical interfaces first; this is critical for
        bonding
    '''
    order = {
        'physical': 0,
        'bond': 1,
        'bridge': 2,
        'vlan': 3,
    }
    content += "auto lo\niface lo inet loopback\n"
    for dnskey, value in network_state.get('dns', {}).items():
        if len(value):
            content += "    dns-{} {}\n".format(dnskey, " ".join(value))

    for iface in sorted(interfaces.values(),
                        key=lambda k: (order[k['type']], k['name'])):

        if content[-2:] != "\n\n":
            content += "\n"
        subnets = iface.get('subnets', {})
        if subnets:
            for index, subnet in enumerate(subnets):
                if content[-2:] != "\n\n":
                    content += "\n"
                iface['index'] = index
                iface['mode'] = subnet['type']
                iface['control'] = subnet.get('control', 'auto')
                subnet_inet = 'inet'
                if subnet_is_ipv6(subnet):
                    subnet_inet += '6'
                iface['inet'] = subnet_inet
                if subnet['type'].startswith('dhcp'):
                    iface['mode'] = 'dhcp'

                # do not emit multiple 'auto $IFACE' lines as older (precise)
                # ifupdown complains
                if "auto %s\n" % (iface['name']) in content:
                    iface['control'] = 'alias'

                content += iface_start_entry(iface)
                content += iface_add_subnet(iface, subnet)
                content += iface_add_attrs(iface, index)

                for route in subnet.get('routes', []):
                    content += render_route(route, indent="    ") + '\n'

        else:
            # ifenslave docs say to auto the slave devices
            if 'bond-master' in iface or 'bond-slaves' in iface:
                content += "auto {name}\n".format(**iface)
            content += "iface {name} {inet} {mode}\n".format(**iface)
            content += iface_add_attrs(iface, 0)

    for route in network_state.get('routes'):
        content += render_route(route)

    # global replacements until v2 format
    content = content.replace('mac_address', 'hwaddress ether')

    # Play nice with others and source eni config files
    content += "\nsource /etc/network/interfaces.d/*.cfg\n"

    return content


def netconfig_passthrough_available(target, pkg_ver=None):
    """
    Determine if curtin can pass networking config
    into the target for cloud-init to consume.

    This is available on cloud-init 0.7.6 and newer
    """
    LOG.debug('Checking in-target cloud-init version')
    if pkg_ver is None:
        pkg_ver = util.get_package_version('cloud-init', target=target)

    if not isinstance(pkg_ver, dict):
        LOG.debug('cloud-init not available in target=%s', target)
        return False

    LOG.debug("get_package_version:\n%s", pkg_ver)
    LOG.debug("cloud-init version is '%s' (major=%s minor=%s micro=%s)",
              pkg_ver.get('semantic_version'), pkg_ver.get('major'),
              pkg_ver.get('minor'), pkg_ver.get('micro'))
    # cloud-init versions < 0.7.6 do _NOT_ want whitespace replaced
    # i.e. 0.7.6 in Trusty.
    if pkg_ver.get('semantic_version', 0) < 706:
        return False

    return True


def netconfig_passthrough_v2_available(target, feature='NETWORK_CONFIG_V2'):
    """
    Determine if curtin can pass v2 network config to in target cloud-init
    """
    LOG.debug('Checking in-target cloud-init features')
    cmd = ("from cloudinit import version;"
           "print({} in getattr(version, 'FEATURES', []))"
           .format(feature))
    with util.ChrootableTarget(target) as in_chroot:

        def run_cmd(cmd):
            (out, _) = in_chroot.subp(cmd, capture=True)
            return out.strip()

        cloud_init_path = util.which('cloud-init', target=target)
        if not cloud_init_path:
            LOG.debug('cloud-init not available in target=%s', target)
            return False

        script_shebang = run_cmd(['head', '-n1', cloud_init_path])
        python = script_shebang.split('/')[-1]
        feature_available = run_cmd([python, '-c', cmd])
        return config.value_as_boolean(feature_available)


def render_netconfig_passthrough(target, netconfig=None):
    """
    Extract original network config and pass it
    through to cloud-init in target
    """
    LOG.debug("generating passthrough netconfig")
    cc = 'etc/cloud/cloud.cfg.d/curtin-networking.cfg'
    if not isinstance(netconfig, dict):
        raise ValueError('Network config must be a dictionary')

    if 'network' not in netconfig:
        raise ValueError('Network config must contain the key \'network\'')

    content = config.dump_config(netconfig)
    cc_passthrough = os.path.sep.join((target, cc,))
    LOG.info('Writing ' + cc_passthrough)
    util.write_file(cc_passthrough, content=content)


def render_network_state(target, network_state):
    LOG.debug("generating eni from netconfig")
    eni = 'etc/network/interfaces'
    netrules = 'etc/udev/rules.d/70-persistent-net.rules'
    cc = 'etc/cloud/cloud.cfg.d/curtin-disable-cloudinit-networking.cfg'

    eni = os.path.sep.join((target, eni,))
    LOG.info('Writing ' + eni)
    util.write_file(eni, content=render_interfaces(network_state))

    netrules = os.path.sep.join((target, netrules,))
    LOG.info('Writing ' + netrules)
    util.write_file(netrules, content=render_persistent_net(network_state))

    cc_disable = os.path.sep.join((target, cc,))
    LOG.info('Writing ' + cc_disable)
    util.write_file(cc_disable, content='network: {config: disabled}\n')


def get_interface_mac(ifname):
    """Returns the string value of an interface's MAC Address"""
    return read_sys_net(ifname, "address", enoent=False)

# vi: ts=4 expandtab syntax=python
