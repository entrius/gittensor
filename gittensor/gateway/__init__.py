# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute pool's gateway: OpenAI-style requests onto leased instances, reserve-or-429 (vault ``26`` §2, ``25``
"Front door types"). Evolves from phase 0's validator-embedded ``gittensor/serving/api.py`` and imports nothing from
it: that package is deleted at cutover."""
