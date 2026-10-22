# Looping decoder memory growth

Hardware encoding and decoding were removed from the project on 2026-09-29
after the streaming pipeline's leak exhausted host memory and swap, causing
host-wide OOM kills and crashes in unrelated applications. Nine ffmpeg
processes were reported using 38 GB of RAM plus 7 GB of swap, with two using
17.5 GB and 15.7 GB. Chrome, a terminal, IntelliJ's embedded browser, and
ffmpeg were killed. There was no container memory limit at the time.

On 2026-09-29, the deployed `autostream:2.2.0` image had FFmpeg 6.1.2-r2,
Intel media-driver 25.2.6-r0, and oneVPL Intel GPU runtime 26.2.4-r0.
Its MJPEG and H.264 QSV decoders retained memory across file loops.

A comparison inside the same container ran each input with `-re -stream_loop -1`,
the same `h264_qsv` encoder, 2 Mbps bitrate, and a null output. This removed
RTSP, UDP, MediaMTX, and downstream consumers from the comparison. Each process
ran for three minutes; RSS samples below are at 10 and 170 seconds, after
startup and before exit.

| Input | Decoder | RSS at 10 s (MiB) | RSS at 170 s (MiB) |
| --- | --- | ---: | ---: |
| rolling_boats.avi | mjpeg_qsv | 91.7 | 331.1 |
| rolling_boats.avi | software | 74.0 | 73.8 |
| cargoship.mp4 | h264_qsv | 62.7 | 97.1 |
| cargoship.mp4 | software | 95.3 | 102.9 |

The MJPEG file lasts 9.967 seconds; hardware decoding added approximately
15 MiB per loop. The H.264 file lasts 20.01 seconds; hardware decoding added
approximately 4 MiB per loop. Software MJPEG decoding plateaued after startup;
software H.264 decoding oscillated with the file contents rather than growing
at each loop. Software decoding with VA-API encoding also stayed bounded.

Decoder-only tests reproduced the growth without H.264 encoding or network
output. The measured leak is in QSV decoding; hardware encoding with software
decoding stayed bounded in the comparisons. All hardware acceleration was
removed as a project reliability decision: transcoding now uses software
decoding and libx264 encoding, with no GPU detection, driver installation,
device mappings, or hardware encoder options. Passthrough, timestamps, and
KLV mapping retain their existing behavior. Software transcoding costs more CPU.

## Upgrade investigation

An isolated Alpine 3.24.1 container tested FFmpeg 8.1.2-r0, Intel media-driver
26.2.1-r0, and oneVPL runtime 25.4.6-r1 against the same inputs and null output.
The three-minute comparison still reproduced memory growth with QSV decoding:

| Decoder | FFmpeg 6.1.2 RSS growth (MiB) | FFmpeg 8.1.2 RSS growth (MiB) |
| --- | ---: | ---: |
| MJPEG | 239.4 | 287.2 |
| H.264 | 34.3 | 32.3 |

Growth is measured between seconds 10 and 170. Software-decoded controls
remained bounded. The released FFmpeg decoder code reinitializes on a flush
and overwrites the frame-buffer pool pointer without freeing the old pool;
this allocation path remains in
[FFmpeg 8.1.2](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/libavcodec/qsvdec.c#L415).
No FFmpeg patch or upgrade was deployed.

The separate 6 GiB Compose memory limit disables container swap and contains
future failures. An OOM can kill any process inside the container, including
the supervisor or MediaMTX; the existing restart policy recovers the stack.

## Verification

Run `make test`. After building and deploying,
inspect the real RTSP and UDP commands: transcoding must use `-c:v libx264`,
with no hardware decoder or device options. Passthrough must use `-c:v copy`.
The container must have no GPU device mappings. Check RTSP playback and that
UDP is still carrying data streams from a KLV source.

## Socket-memory investigation

`python3 socket_memory.py` captures one snapshot per minute into rotating
`.generated/socket-memory.jsonl` files. It records:

- Host memory, TCP/UDP socket memory, and allocated bytes in the skbuff slab
  caches (read through a root Docker exec when the host denies slab access).
- Host sockets via `ss -tunapme`, including queue sizes and socket memory.
- Every running container's TCP/UDP tables, namespace socket inodes and PID
  owners, cgroup memory breakdown, and OOM events. The socket tables encode
  transmit/receive queue lengths in hexadecimal.

Host `ss` alone misses sockets in bridge-network containers. Cgroup `sock`,
`kernel`, and `slab` counters help attribute growth; compare socket-table inodes
to the captured process fd symlinks. A restarting container can fail a snapshot;
the error is recorded and capture continues. Packet buffers can also accumulate
outside socket queues, so quiet queues do not rule out a kernel/network leak.
Socket ownership is best effort: minimal images may lack the listing tools,
and fd access for another user can be denied. Host PID/fd capture supplements
the container listings and records access errors without stopping collection.
The earlier 41 GiB socket-buffer growth has not been reproduced or explained.
