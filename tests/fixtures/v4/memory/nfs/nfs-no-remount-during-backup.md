---
id: nfs-no-remount-during-backup
title: Do not remount /mnt/media during a backup window
type: constraint
scope: ["mount:/mnt/media", "backup:nightly"]
source: operator-stated
---
Never unmount or remount /mnt/media while the nightly backup is running. Wait for the job to finish or
ask the operator.
