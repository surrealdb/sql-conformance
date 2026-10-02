# SurrealDB SQL Conformance Test Suite & Oracles

This repository hosts SQL conformance test corpora, canonical AST trees, validation oracles, and test harness tooling for SurrealDB's SQL front ends (`surrealdb-pgsql`, `surrealdb-mysql`).

## Repository Organization

- **`oracles/`**: Tooling and binaries that invoke reference parsers to dump canonical AST representations:
  - `pg_query-v17`: libpg_query 6.2.1 (PostgreSQL 17.7 parser)
  - `pg_query-v18`: libpg_query revision `ad0c87ed` (PostgreSQL 18.6 parser)
- **`suites/`**: Pinned regression test suites and precomputed canonical AST datasets:
  - `postgresql-v17`: PostgreSQL 17 test suite and canonical JSON AST trees
  - `postgresql-v18`: PostgreSQL 18 test suite and canonical JSON AST trees
- **`tooling/`**: Helper utilities for regression suite extraction, AST diffing, and ratchet enforcement:
  - `fetch-pg-tests`: Extracts and splits statements from upstream PostgreSQL regression suites
  - `json-diff`: Fast canonical AST comparator
  - `ratchet`: Metric ratcheting validator

## Relationship to SurrealDB Main Repository

The SurrealDB main codebase (`surrealdb/surrealdb`) does **not** include heavy reference parsers (such as `pg_query`, `clang-sys`, `bindgen`, or `prost`) in its dependency tree. Instead:
1. Every dataset in this repository is accompanied by precomputed canonical AST JSON and a `MANIFEST.sha256`.
2. SurrealDB crates pin a specific commit and manifest hash in their local `conformance.lock`.
3. CI jobs fetch and verify the precomputed test suites using sparse checkouts and verify their SHA-256 hashes against the lockfile.
4. Heavy live oracle jobs run in dedicated workflows using this repository.

## Licensing

SurrealDB-authored code and tooling in this repository are licensed under the [Apache-2.0](LICENSE) license.
Third-party test suites and materials retain their original upstream licenses (e.g. PostgreSQL License).
