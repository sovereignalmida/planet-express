---
id: nfs-stale-handle-symptoms
title: Stale NFS file handle while containers look healthy
type: fact
scope: ["mount:/mnt/media", "stack:media"]
source: observed
evidence: ["incident-example-3"]
review_after: 2027-01-13
---
After the storage server restarts, reads under /mnt/media fail with "Stale file handle" while the media
containers stay up and healthy. The mount still appears in the mount table. Listing the directory on the
host fails; listing it inside a container fails the same way.
