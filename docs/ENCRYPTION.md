# Encryption at Rest

## Current State

### SQLite Databases
The Corporate Bot stores data in two SQLite databases:

- **conversations.db** (bot-data volume): Messages, facts, media cache, audit log,
  credential store, user tokens (Fernet-encrypted), RAG embeddings
- **pm.db** (bot-data volume): Project management data (tickets, sprints, projects)

**SQLite files are NOT encrypted at rest.** The on-disk `.db` files are plaintext SQLite.

However, sensitive fields within the database ARE encrypted:
- `user_tokens` table: OAuth tokens encrypted with Fernet (AES-128-CBC) via
  `bot/auth/token_vault.py`. Key sourced from `TOKEN_ENCRYPTION_KEY` env var.
- `credential_store` table: Credentials encrypted with the same Fernet key.

### Docker Volumes
Bot data lives in Docker named volumes (`bot-data`, `media-cache`, `claude-sessions`).
These use the default `local` driver — stored as regular files on the host filesystem
at `/var/lib/docker/volumes/`.

### Backup Archives
Backup files (`.7z`) are encrypted with AES-256 via 7-Zip, using a password from
`.backup-password`. This provides encryption at rest for off-host copies (Mac, GDrive).

## Options for Full Encryption at Rest

### Option 1: LUKS/dm-crypt Volume-Level Encryption (Recommended)

Encrypt the host partition or logical volume where Docker stores its volumes.

**Pros:**
- Transparent to the application (no code changes)
- Encrypts everything: SQLite, config files, session data, media cache
- OS-level — well-tested, hardware-accelerated (AES-NI)
- Protects against physical disk theft

**Cons:**
- Requires host-level setup (not containerized)
- Data is decrypted when the system is running (protects at-rest only)
- Key management via LUKS keyslot (passphrase or key file at boot)

**Setup:**
```bash
# Create encrypted partition (one-time)
sudo cryptsetup luksFormat /dev/sdX
sudo cryptsetup luksOpen /dev/sdX bot-data-crypt
sudo mkfs.ext4 /dev/mapper/bot-data-crypt
sudo mount /dev/mapper/bot-data-crypt /var/lib/docker/volumes/

# Auto-unlock at boot (key file approach)
sudo dd if=/dev/urandom of=/root/.bot-data-key bs=4096 count=1
sudo chmod 400 /root/.bot-data-key
sudo cryptsetup luksAddKey /dev/sdX /root/.bot-data-key
# Add to /etc/crypttab: bot-data-crypt /dev/sdX /root/.bot-data-key luks
```

### Option 2: SQLCipher (Database-Level Encryption)

Replace `aiosqlite` with `sqlcipher3` for per-database encryption.

**Pros:**
- Encrypts individual database files (AES-256-CBC)
- Portable — encrypted `.db` files can be moved between hosts
- Selective — can encrypt only sensitive databases

**Cons:**
- Requires code changes (connection string / pragma key)
- New dependency: `sqlcipher3` (compiled C library)
- Performance overhead (~5-15% for typical workloads)
- Media cache and config files are NOT covered

**Code changes required:**
```python
# Current (aiosqlite)
async with aiosqlite.connect(DB_PATH) as db:
    ...

# With SQLCipher
import pysqlcipher3.dbapi2 as sqlcipher
# Or via aiosqlite with sqlcipher backend
async with aiosqlite.connect(DB_PATH) as db:
    await db.execute("PRAGMA key = ?", (encryption_key,))
    ...
```

### Option 3: Docker Volume Encryption Plugin

Use Docker volume plugins that provide encryption (e.g., `convoy`, `rexray`).

**Pros:**
- Container-level isolation
- Managed via Docker Compose

**Cons:**
- Adds operational complexity
- Plugin maintenance burden
- Less battle-tested than LUKS

## Recommendation

**Use LUKS/dm-crypt (Option 1)** for production deployments:

1. It requires zero application code changes
2. It encrypts ALL persistent data uniformly
3. It is the industry standard for Linux server encryption
4. Hardware acceleration (AES-NI) means negligible performance impact

The existing Fernet encryption for user tokens (Option 2 pattern, field-level) provides
defense-in-depth: even if LUKS is compromised, individual tokens require the
`TOKEN_ENCRYPTION_KEY` to decrypt.

For compliance requirements that mandate database-level encryption specifically,
SQLCipher (Option 2) can be added as a secondary layer on top of LUKS.

## Current Mitigations

Even without full disk encryption, the following protections are in place:

1. **Fernet-encrypted tokens**: User OAuth tokens in `user_tokens` table
2. **Encrypted backups**: 7-Zip AES-256 for off-host backup archives
3. **Container isolation**: Docker namespaces prevent cross-container access
4. **Non-root execution**: Bot runs as `botuser` (UID 10001), not root
5. **File permissions**: Sensitive files (`.env`, session files) are mode 600
6. **Volume isolation**: Staging uses separate named volumes from production
