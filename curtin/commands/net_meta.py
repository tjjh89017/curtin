#   Copyright (C) 2013 Canonical Ltd.
#
#   Author: Scott Moser <scott.moser@canonical.com>
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

import argparse
import os
import sys

from curtin import net
import curtin.util as util

from . import populate_one_subcmd

DEVNAME_ALIASES = ['connected', 'configured', 'netboot']


def network_device(value):
    if value in DEVNAME_ALIASES:
        return value
    if (value.startswith('eth') or
            (value.startswith('en') and len(value) == 3)):
        return value
    raise argparse.ArgumentTypeError("%s does not look like a netdev name")


def resolve_alias(alias):
    if alias == "connected":
        alldevs = net.get_devicelist()
        return [d for d in alldevs if
                net.is_physical(d) and net.is_up(d)]
    elif alias == "configured":
        alldevs = net.get_devicelist()
        return [d for d in alldevs if
                net.is_physical(d) and net.is_up(d) and net.is_connected(d)]
    elif alias == "netboot":
        # should read /proc/cmdline here for BOOTIF
        raise NotImplemented("netboot alias not implemented")
    else:
        raise ValueError("'%s' is not an alias: %s", alias, DEVNAME_ALIASES)


def interfaces_basic_dhcp(devices):
    content = '\n'.join(
        [("# This file describes the network interfaces available on "
         "your system"),
         "# and how to activate them. For more information see interfaces(5).",
         "",
         "# The loopback network interface",
         "auto lo",
         "iface lo inet loopback",
         ])

    for d in devices:
        content += '\n'.join(("", "", "auto %s" % d,
                              "iface %s inet dhcp" % d,))
    content += "\n"

    return content


def interfaces_custom(args):
    content = '\n'.join(
        [("# Autogenerated interfaces from net-meta custom mode"),
         "",
         "# The loopback network interface",
         "auto lo",
         "iface lo inet loopback",
         "",
         ])

    command_handlers = {
        'physical': handle_physical,
        'vlan': handle_vlan,
        'bond': handle_bond,
        'bridge': handle_bridge,
        'route': handle_route,
        'nameserver': handle_nameserver,
    }

    state = util.load_command_environment()
    cfg = config.load_command_config(args, state)

    network_config = cfg.get('network', [])
    if not network_config:
        raise Exception("network configuration is required by mode '%s' "
                        "but not provided in the config file" % 'custom')

    for command in network_config:
        handler = command_handlers.get(command['type'])
        if not handler:
            raise ValueError("unknown command type '%s'" % command['type'])
        content += handler(command, args)
        content = content.replace('\n\n\n', '\n\n')

    return content


def handle_vlan(command, args):
    '''
        auto eth0.222
        iface eth0.222 inet static
                address 10.10.10.1
                netmask 255.255.255.0
                vlan-raw-device eth0
    '''
    content = handle_physical(command, args)[:-1]
    content += "    vlan-raw-device {}".format(command['vlan_link'])

    return content


def handle_bond(command, args):
    '''
#/etc/network/interfaces
auto eth0
iface eth0 inet manual

auto eth1
iface eth1 inet manual

auto bond0
iface bond0 inet static
     address 192.168.0.10
     gateway 192.168.0.1
     netmask 255.255.255.0
     bond-mode 802.3ad
     bond-miimon 100
     bond-downdelay 200
     bond-updelay 200
     bond-lacp-rate 4
    '''
    # write out bondX iface stanza and options
    content = handle_physical(command, args)[:-1]
    params = command.get('params', [])
    for param, value in params.items():
        content += "    {} {}\n".format(param, value)

    content += "\n"

    # now write out slaved iface stanzas
    for slave in command['bond_interfaces']:
        content += "auto {}\n".format(slave)
        content += "iface {} inet manual\n".format(slave)
        content += "    bond-master {}\n\n".format(command['name'])

    return content


def handle_bridge(command, args):
    '''
        auto br0
        iface br0 inet static
                address 10.10.10.1
                netmask 255.255.255.0
                bridge_ports eth0 eth1
                bridge_stp off
                bridge_fd 0
                bridge_maxwait 0

    '''
    bridge_params = [
        "bridge_ports",
        "bridge_ageing",
        "bridge_bridgeprio",
        "bridge_fd",
        "bridge_gcint",
        "bridge_hello",
        "bridge_hw",
        "bridge_maxage",
        "bridge_maxwait",
        "bridge_pathcost",
        "bridge_portprio",
        "bridge_stp",
        "bridge_waitport",
    ]

    content = handle_physical(command, args)[:-1]
    content += "    bridge_ports %s\n" % (
        " ".join(command['bridge_interfaces']))
    params = command.get('params', [])
    for param, value in params.items():
        if param in bridge_params:
            content += "    {} {}\n".format(param, value)

    return content


def cidr2mask(cidr):
    mask = [0, 0, 0, 0]
    for i in list(range(0, cidr)):
        idx = int(i / 8)
        mask[idx] = mask[idx] + (1 << (7 - i % 8))
    return ".".join([str(x) for x in mask])


def handle_route(command, args):
    content = "\n"
    network, cidr = command['destination'].split("/")
    netmask = cidr2mask(int(cidr))
    command['network'] = network
    command['netmask'] = netmask
    content += "up route add"
    mapping = {
        'network': '-net',
        'netmask': 'netmask',
        'gateway': 'gw',
        'metric': 'metric',
    }
    for k in ['network', 'netmask', 'gateway', 'metric']:
        if k in command:
            content += " %s %s" % (mapping[k], command[k])

    content += '\n'
    return content


def handle_nameserver(command, args):
    content = "\n"
    if 'address' in command:
        content += "dns-nameserver {address}\n".format(**command)
    if 'search' in command:
        content += "dns-search {search}\n".format(**command)

    return content


def handle_physical(command, args):
    '''
    command = {
        'type': 'physical',
        'mac_address': 'c0:d6:9f:2c:e8:80',
        'name': 'eth0',
        'subnets': [
            {'type': 'dhcp4'}
         ]
    }
    '''
    ctxt = {
        'name': command.get('name'),
        'inet': 'inet',
        'mode': 'manual',
        'mtu': command.get('mtu'),
        'address': None,
        'gateway': None,
        'subnets': command.get('subnets'),
    }

    content = ""
    content += "auto {name}\n".format(**ctxt)
    subnets = command.get('subnets', {})
    if subnets:
        for index, subnet in zip(range(0, len(subnets)), subnets):
            ctxt['index'] = index
            ctxt['mode'] = subnet['type']
            if ctxt['mode'].endswith('6'):
                ctxt['inet'] += '6'
            elif ctxt['mode'] == 'static' and ":" in subnet['address']:
                ctxt['inet'] += '6'
            if ctxt['mode'].startswith('dhcp'):
                ctxt['mode'] = 'dhcp'

            if index == 0:
                content += "iface {name} {inet} {mode}\n".format(**ctxt)
            else:
                content += \
                    "iface {name}:{index} {inet} {mode}\n".format(**ctxt)

            if 'mtu' in ctxt and ctxt['mtu'] and index == 0:
                content += "    mtu {mtu}\n".format(**ctxt)
            if 'address' in subnet:
                content += "    address {address}\n".format(**subnet)
            if 'gateway' in subnet:
                content += "    gateway {gateway}\n".format(**subnet)
            content += "\n"
    else:
        content += "iface {name} {inet} {mode}\n\n".format(**ctxt)

    # for physical interfaces ,write out a persist net udev rule
    if command['type'] == 'physical' and \
       'name' in command and 'mac_address' in command:
        udev_line = generate_udev_rule(command['name'],
                                       command['mac_address'])
        persist_net = 'etc/udev/rules.d/70-persistent-net.rules'
        netrules = os.path.sep.join((args.target, persist_net,))
        util.ensure_dir(os.path.dirname(netrules))
        with open(netrules, 'a+') as f:
            f.write(udev_line)

    return content


def compose_udev_equality(key, value):
    """Return a udev comparison clause, like `ACTION=="add"`."""
    assert key == key.upper()
    return '%s=="%s"' % (key, value)


def compose_udev_attr_equality(attribute, value):
    """Return a udev attribute comparison clause, like `ATTR{type}=="1"`."""
    assert attribute == attribute.lower()
    return 'ATTR{%s}=="%s"' % (attribute, value)


def compose_udev_setting(key, value):
    """Return a udev assignment clause, like `NAME="eth0"`."""
    assert key == key.upper()
    return '%s="%s"' % (key, value)


def generate_udev_rule(interface, mac):
    """Return a udev rule to set the name of network interface with `mac`.

    The rule ends up as a single line looking something like:

    SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*",
    ATTR{address}="ff:ee:dd:cc:bb:aa", NAME="eth0"
    """
    rule = ', '.join([
        compose_udev_equality('SUBSYSTEM', 'net'),
        compose_udev_equality('ACTION', 'add'),
        compose_udev_equality('DRIVERS', '?*'),
        compose_udev_attr_equality('address', mac),
        compose_udev_setting('NAME', interface),
        ])
    return '%s\n' % rule


def net_meta(args):
    #    curtin net-meta --devices connected dhcp
    #    curtin net-meta --devices configured dhcp
    #    curtin net-meta --devices netboot dhcp
    #    curtin net-meta --devices connected custom

    # if network-config hook exists in target,
    # we do not run the builtin
    if util.run_hook_if_exists(args.target, 'network-config'):
        sys.exit(0)

    state = util.load_command_environment()
    cfg = config.load_command_config(args, state)
    if cfg.get("network") is not None:
        args.mode = "custom"

    eni = "etc/network/interfaces"
    if args.mode == "auto":
        if not args.devices:
            args.devices = ["connected"]

        t_eni = None
        if args.target:
            t_eni = os.path.sep.join((args.target, eni,))
            if not os.path.isfile(t_eni):
                t_eni = None

        if t_eni:
            args.mode = "copy"
        else:
            args.mode = "dhcp"

    devices = []
    if args.devices:
        for dev in args.devices:
            if dev in DEVNAME_ALIASES:
                devices += resolve_alias(dev)
            else:
                devices.append(dev)

    if args.mode == "copy":
        if not args.target:
            raise argparse.ArgumentTypeError("mode 'copy' requires --target")

        t_eni = os.path.sep.join((args.target, "etc/network/interfaces",))
        with open(t_eni, "r") as fp:
            content = fp.read()

    elif args.mode == "dhcp":
        content = interfaces_basic_dhcp(devices)
    elif args.mode == 'custom':
        content = interfaces_custom(args)

    if args.output == "-":
        sys.stdout.write(content)
    else:
        with open(args.output, "w") as fp:
            fp.write(content)


CMD_ARGUMENTS = (
    ((('-D', '--devices'),
      {'help': 'which devices to operate on', 'action': 'append',
       'metavar': 'DEVICE', 'type': network_device}),
     (('-o', '--output'),
      {'help': 'file to write to. defaults to env["OUTPUT_INTERFACES"] or "-"',
       'metavar': 'IFILE', 'action': 'store',
       'default': os.environ.get('OUTPUT_INTERFACES', "-")}),
     (('-t', '--target'),
      {'help': 'operate on target. default is env[TARGET_MOUNT_POINT]',
       'action': 'store', 'metavar': 'TARGET',
       'default': os.environ.get('TARGET_MOUNT_POINT')}),
     ('mode', {'help': 'meta-mode to use',
               'choices': ['dhcp', 'copy', 'auto', 'custom']})
     )
)


def POPULATE_SUBCMD(parser):
    populate_one_subcmd(parser, CMD_ARGUMENTS, net_meta)

# vi: ts=4 expandtab syntax=python
