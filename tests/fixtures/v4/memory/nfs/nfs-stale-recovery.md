---
id: nfs-stale-recovery
title: Recover a stale NFS mount under the media stack
type: procedure
scope: ["mount:/mnt/media", "stack:media"]
source: operator-stated
evidence: ["incident-example-3"]
---
Stop consumers, remount, start consumers. Order matters.
1. Confirm no backup job is running against the mount.
   check: the backup status shows idle.
2. Stop the containers of stack media.
   check: no container of the stack is running.
3. Unmount /mnt/media with a lazy unmount, then mount it again.
   check: the mount table shows /mnt/media once and a directory listing succeeds.
4. Start the stack in dependency order.
   check: a media file is readable from inside the primary container.
