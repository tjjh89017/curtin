# This file is part of curtin. See LICENSE file for copyright and license info.

import os
import sys

from .. import log
import curtin.net as net
import curtin.util as util
from curtin import config
from curtin import paths
from . import populate_one_subcmd


LOG = log.LOG

IFUPDOWN_IPV6_MTU_PRE_HOOK = """#!/bin/bash -e
# injected by curtin installer

[ "${IFACE}" != "lo" ] || exit 0

# Trigger only if MTU configured
[ -n "${IF_MTU}" ] || exit 0

read CUR_DEV_MTU </sys/class/net/${IFACE}/mtu ||:
read CUR_IPV6_MTU </proc/sys/net/ipv6/conf/${IFACE}/mtu ||:
[ -n "${CUR_DEV_MTU}" ] && echo ${CUR_DEV_MTU} > /run/network/${IFACE}_dev.mtu
[ -n "${CUR_IPV6_MTU}" ] &&
  echo ${CUR_IPV6_MTU} > /run/network/${IFACE}_ipv6.mtu
exit 0
"""

IFUPDOWN_IPV6_MTU_POST_HOOK = """#!/bin/bash -e
# injected by curtin installer

[ "${IFACE}" != "lo" ] || exit 0

# Trigger only if MTU configured
[ -n "${IF_MTU}" ] || exit 0

read PRE_DEV_MTU </run/network/${IFACE}_dev.mtu ||:
read CUR_DEV_MTU </sys/class/net/${IFACE}/mtu ||:
read PRE_IPV6_MTU </run/network/${IFACE}_ipv6.mtu ||:
read CUR_IPV6_MTU </proc/sys/net/ipv6/conf/${IFACE}/mtu ||:

if [ "${ADDRFAM}" = "inet6" ]; then
  # We need to check the underlying interface MTU and
  # raise it if the IPV6 mtu is larger
  if [ ${CUR_DEV_MTU} -lt ${IF_MTU} ]; then
      ip link set ${IFACE} mtu ${IF_MTU}
  fi
  # sysctl -q -e -w net.ipv6.conf.${IFACE}.mtu=${IF_MTU}
  echo ${IF_MTU} >/proc/sys/net/ipv6/conf/${IFACE}/mtu ||:

elif [ "${ADDRFAM}" = "inet" ]; then
  # handle the clobber case where inet mtu changes v6 mtu.
  # ifupdown will already have set dev mtu, so lower mtu
  # if needed.  If v6 mtu was larger, it get's clamped down
  # to the dev MTU value.
  if [ ${PRE_IPV6_MTU} -lt ${CUR_IPV6_MTU} ]; then
    # sysctl -q -e -w net.ipv6.conf.${IFACE}.mtu=${PRE_IPV6_MTU}
    echo ${PRE_IPV6_MTU} >/proc/sys/net/ipv6/conf/${IFACE}/mtu ||:
  fi
fi
exit 0
"""


def apply_net(target, network_state=None, network_config=None):
    if network_state is None and network_config is None:
        raise ValueError("Must provide network_config or network_state")

    if target is None:
        raise ValueError("target cannot be None.")

    passthrough = False
    if network_state:
        # NB: we cannot support passthrough until curtin can convert from
        # network_state to network-config yaml
        ns = net.network_state.from_state_file(network_state)
        raise ValueError('Not Supported; curtin lacks a network_state to '
                         'network_config converter.')
    elif network_config:
        netcfg = config.load_config(network_config)

        # curtin will pass-through the netconfig into the target
        # for rendering at runtime unless the target OS does not
        # support NETWORK_CONFIG_V2 feature.
        LOG.info('Checking cloud-init in target [%s] for network '
                 'configuration passthrough support.', target)
        try:
            passthrough = net.netconfig_passthrough_available(target)
        except util.ProcessExecutionError:
            LOG.warning('Failed to determine if passthrough is available')

        if passthrough:
            LOG.info('Passing network configuration through to target: %s',
                     target)
            net.render_netconfig_passthrough(target, netconfig=netcfg)
        else:
            ns = net.parse_net_config_data(netcfg.get('network', {}))

            if ns is None:
                return

    if not passthrough:
        LOG.info('Rendering network configuration in target')
        net.render_network_state(target=target, network_state=ns)

    _maybe_remove_legacy_eth0(target)
    _disable_ipv6_privacy_extensions(target)
    _patch_ifupdown_ipv6_mtu_hook(target)


def _patch_ifupdown_ipv6_mtu_hook(target,
                                  prehookfn="etc/network/if-pre-up.d/mtuipv6",
                                  posthookfn="etc/network/if-up.d/mtuipv6"):

    contents = {
        'prehook': IFUPDOWN_IPV6_MTU_PRE_HOOK,
        'posthook': IFUPDOWN_IPV6_MTU_POST_HOOK,
    }

    hookfn = {
        'prehook': prehookfn,
        'posthook': posthookfn,
    }

    for hook in ['prehook', 'posthook']:
        fn = hookfn[hook]
        cfg = paths.target_path(target, path=fn)
        LOG.info('Injecting fix for ipv6 mtu settings: %s', cfg)
        util.write_file(cfg, contents[hook], mode=0o755)


def _disable_ipv6_privacy_extensions(target,
                                     path="etc/sysctl.d/10-ipv6-privacy.conf"):

    """Ubuntu server image sets a preference to use IPv6 privacy extensions
       by default; this races with the cloud-image desire to disable them.
       Resolve this by allowing the cloud-image setting to win. """

    LOG.debug('Attempting to remove ipv6 privacy extensions')
    cfg = paths.target_path(target, path=path)
    if not os.path.exists(cfg):
        LOG.warn('Failed to find ipv6 privacy conf file %s', cfg)
        return

    bmsg = "Disabling IPv6 privacy extensions config may not apply."
    try:
        contents = util.load_file(cfg)
        known_contents = ["net.ipv6.conf.all.use_tempaddr = 2",
                          "net.ipv6.conf.default.use_tempaddr = 2"]
        lines = [f.strip() for f in contents.splitlines()
                 if not f.startswith("#")]
        if lines == known_contents:
            LOG.info('Removing ipv6 privacy extension config file: %s', cfg)
            util.del_file(cfg)
            msg = "removed %s with known contents" % cfg
            curtin_contents = '\n'.join(
                ["# IPv6 Privacy Extensions (RFC 4941)",
                 "# Disabled by curtin",
                 "# net.ipv6.conf.all.use_tempaddr = 2",
                 "# net.ipv6.conf.default.use_tempaddr = 2"])
            util.write_file(cfg, curtin_contents)
        else:
            LOG.debug('skipping removal of %s, expected content not found',
                      cfg)
            LOG.debug("Found content in file %s:\n%s", cfg, lines)
            LOG.debug("Expected contents in file %s:\n%s", cfg, known_contents)
            msg = (bmsg + " '%s' exists with user configured content." % cfg)
    except Exception as e:
        msg = bmsg + " %s exists, but could not be read. %s" % (cfg, e)
        LOG.exception(msg)
        raise


def _maybe_remove_legacy_eth0(target,
                              path="etc/network/interfaces.d/eth0.cfg"):
    """Ubuntu cloud images previously included a 'eth0.cfg' that had
       hard coded content.  That file would interfere with the rendered
       configuration if it was present.

       if the file does not exist do nothing.
       If the file exists:
         - with known content, remove it and warn
         - with unknown content, leave it and warn
    """

    cfg = paths.target_path(target, path=path)
    if not os.path.exists(cfg):
        LOG.warn('Failed to find legacy network conf file %s', cfg)
        return

    bmsg = "Dynamic networking config may not apply."
    try:
        contents = util.load_file(cfg)
        known_contents = ["auto eth0", "iface eth0 inet dhcp"]
        lines = [f.strip() for f in contents.splitlines()
                 if not f.startswith("#")]
        if lines == known_contents:
            util.del_file(cfg)
            msg = "removed %s with known contents" % cfg
        else:
            msg = (bmsg + " '%s' exists with user configured content." % cfg)
    except Exception:
        msg = bmsg + " %s exists, but could not be read." % cfg
        LOG.exception(msg)
        raise

    LOG.warn(msg)


def apply_net_main(args):
    #  curtin apply_net [--net-state=/config/netstate.yml] [--target=/]
    #                   [--net-config=/config/maas_net.yml]
    state = util.load_command_environment()

    log.basicConfig(stream=args.log_file, verbosity=1)

    if args.target is not None:
        state['target'] = args.target

    if args.net_state is not None:
        state['network_state'] = args.net_state

    if args.net_config is not None:
        state['network_config'] = args.net_config

    if state['target'] is None:
        sys.stderr.write("Unable to find target.  "
                         "Use --target or set TARGET_MOUNT_POINT\n")
        sys.exit(2)

    if not state['network_config'] and not state['network_state']:
        sys.stderr.write("Must provide at least config or state\n")
        sys.exit(2)

    LOG.info('Applying network configuration')
    apply_net(target=state['target'],
              network_state=state['network_state'],
              network_config=state['network_config'])

    LOG.info('Applied network configuration successfully')
    sys.exit(0)


CMD_ARGUMENTS = (
    ((('-s', '--net-state'),
     {'help': ('file to read containing network state. '
               'defaults to env["OUTPUT_NETWORK_STATE"]'),
      'metavar': 'NETSTATE', 'action': 'store',
      'default': os.environ.get('OUTPUT_NETWORK_STATE')}),
     (('-t', '--target'),
      {'help': ('target filesystem root to configure networking to. '
                'default is env["TARGET_MOUNT_POINT"]'),
       'metavar': 'TARGET', 'action': 'store',
       'default': os.environ.get('TARGET_MOUNT_POINT')}),
     (('-c', '--net-config'),
      {'help': ('file to read containing curtin network config.'
                'defaults to env["OUTPUT_NETWORK_CONFIG"]'),
       'metavar': 'NETCONFIG', 'action': 'store',
       'default': os.environ.get('OUTPUT_NETWORK_CONFIG')})))


def POPULATE_SUBCMD(parser):
    populate_one_subcmd(parser, CMD_ARGUMENTS, apply_net_main)

# vi: ts=4 expandtab syntax=python
