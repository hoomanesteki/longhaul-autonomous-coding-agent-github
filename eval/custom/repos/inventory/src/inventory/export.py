"""Write stock levels out as text."""

import json

import yaml

from inventory.store import Inventory


def dump_json(inventory: Inventory) -> str:
    """Return the stock levels as JSON, sorted by SKU."""
    return json.dumps(inventory.snapshot(), indent=2, sort_keys=True)


def dump_yaml(inventory: Inventory) -> str:
    """Return the stock levels as YAML, sorted by SKU."""
    return yaml.safe_dump(inventory.snapshot(), sort_keys=True)
