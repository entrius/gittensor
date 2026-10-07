# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt rent``: the customer's CLI for GPU box rentals (vault 29 §6)."""

from gittensor.cli.rent_commands.rent import rent_group


def register_rent_commands(cli):
    cli.add_command(rent_group, name='rent')
