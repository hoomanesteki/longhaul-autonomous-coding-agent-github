import json

import yaml
from inventory import Inventory, dump_json, dump_yaml


def stocked() -> Inventory:
    inv = Inventory()
    inv.add("widget", 3)
    inv.add("gadget", 1)
    return inv


def test_dump_json_lists_every_sku() -> None:
    assert json.loads(dump_json(stocked())) == {"gadget": 1, "widget": 3}


def test_dump_yaml_lists_every_sku() -> None:
    assert yaml.safe_load(dump_yaml(stocked())) == {"gadget": 1, "widget": 3}
