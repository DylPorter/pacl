# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Dylan Porter
"""Proactive agent coordination layer.

Agents are first-class principals. Identity is per-agent: ed25519 keypair +
capability advertisement + optional endpoint. Person-level grouping is
app-layer metadata, not protocol concern.
"""

__version__ = "0.0.1"
