"""Is a real system wired? Ask the vault, never .env.

A connection's address and sign-in live only in the vault (Studio's
Infrastructure page). Pod code that needs to know "is the ATS wired?" checks
whether the vault record exists — it never reads an address, a token or an
on/off switch from the environment.
"""
from __future__ import annotations


async def vault_record_exists(name: str) -> bool:
    """True when the vault holds a connection record called ``name``.

    Only "no such record" (or no vault at all: unit tests, a bare script)
    means "not wired": ``connection_rows`` raises ``UnresolvedVaultRef`` and
    the dev-store fallback runs and says so. A record this agent may not read
    (``SecretNotAllowed``, a PermissionError) still exists, so it counts as
    wired and the real call reports who may use it. Any other error (the
    store is unreachable, for example) is raised: an outage must never quietly
    switch a real system off and the dev store on.
    """
    from leafmesh import connection_rows
    from leafmesh.vault.refs import UnresolvedVaultRef
    try:
        await connection_rows(name)
    except UnresolvedVaultRef:
        return False
    except PermissionError:
        return True
    return True


async def any_wired(*names: str) -> bool:
    for n in names:
        if await vault_record_exists(n):
            return True
    return False
