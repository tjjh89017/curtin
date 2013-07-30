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

import yaml


def merge_config_fp(cfgin, fp):
    merge_config(cfgin, yaml.safe_load(fp))


def merge_config_str(cfgin, cfgstr):
    merge_config(cfgin, yaml.safe_load(cfgstr))


def merge_config(cfg, cfg2):
    # merge cfg2 over the top of cfg
    for k, v in cfg2.items():
        if isinstance(v, dict) and isinstance(cfg.get(k, None), dict):
            merge_config(cfg[k], v)
        else:
            cfg[k] = v


def cmdarg2cfg(cmdarg, delim="/"):
    if '=' not in cmdarg:
        raise ValueError('no "=" in "%s"' % cmdarg)

    key, val = cmdarg.split("=", 2)
    cfg = {}
    cur = cfg
    items = key.split(delim)
    for item in items[:-1]:
        cur[item] = {}
        cur = cur[item]

    cur[items[-1]] = val
    return cfg