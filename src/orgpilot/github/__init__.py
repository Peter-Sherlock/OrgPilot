"""Read-only GitHub access contracts (ADR-0015 C2).

Payload models mirror the platform objects OrgPilot materializes; every model
is frozen and carries the platform's stable id so renames of titles or branch
names never create a second object identity. There are no write operations in
this package by construction — authentication token exchange is a separate
concern (:class:`CredentialProvider`) and never mixed into content reads.
"""
