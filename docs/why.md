# Why g64conv exists

Somebody hands you a USB stick with a Genetec export on it: a `.g64x` archive
or a folder of `.g64` segments. You want an MP4. This page is what was
available for that on 2026-09-07, checked against each vendor's own pages and
the actual source trees, and it is the reason this tool was written.

**Short version:** two other free routes exist and both have a hard gate.
g64conv is the only free option that runs on macOS and Linux, needs no vendor
software, reads the whole `.g64x` archive (every camera, segments joined), and
does not depend on how the file was exported.

## What was checked

| Tool | Cost | Platform | `.g64x` archives | Catch |
|---|---|---|---|---|
| **g64conv** | Free, MIT | macOS, Linux, Windows | Yes: every camera, continuation segments joined | Needs Python 3.10+ and ffmpeg; installs from git (no PyPI release yet); cannot read SRTP-encrypted exports |
| Genetec Video Player | Free download, no login | Windows only (Genetec's FAQ: incompatible with macOS, Linux, Android) | Plays; re-exports one sequence at a time | "Save as" MP4/ASF is only available if the exporter ticked *Allow the exported video file to be re-exported*; password-protected files can never be re-exported; the installer is a 1.3 GB executable |
| VLC `g64rtp` demuxer | Free, LGPL | Source only | No: the demuxer reads raw `.g64`, it has no zip/`.g64x` code | Lives only in the unreleased 4.0-dev branch; absent from 3.0.x, and the latest Mac build is 3.0.23 |
| Genetec Security Desk | Licensed | Windows, inside a Security Center deployment | Yes (Video file explorer, Convert) | Only the operator who owns the system has it. The person who received the export does not |
| Axon Evidence / Axon Investigate | Agency licence | Cloud / Windows | Lists `.g64`, `.g64a`, `.g64m`, `.g64x` | Sold to law-enforcement agencies, not to the public |
| Amped FIVE / Amped Replay | Quote-based | Windows 10/11 | G64 and G64A muxer pairs since the May 2026 update | Forensic products |
| Garrett Discovery, Reduct, Magnet DVR Examiner | Service / enterprise add-on / licence | Service, cloud, Windows | Case by case | Reduct otherwise tells you to use Genetec's player |
| "Online G64 converters" (convert.guru, 101convert, converthelper, docpose) | "Free" | Web | No | SEO pages. One offers to "extract text" from the archive, another says it cannot read G64 files and then describes the Commodore 64 disk image of the same name. Do not upload evidence to these |

No other open-source implementation was found. A GitHub repository search for
`g64 genetec` returns exactly one result, this repository. PyPI has no `g64`
package. ffmpeg 9.0.1 ships no g64 demuxer. The search is trustworthy because
the same query finds g64conv itself.

## What "free and easy" looks like in practice

g64conv, any OS:

```
brew install ffmpeg          # or apt / winget
pipx install "git+https://github.com/barkleesanders/g64conv"
g64conv convert export.g64x  # or: g64conv gui
```

One MP4 per camera, H.264 copied bit-exact, the camera's own timestamps,
frame count verified with ffprobe, a JSON report, fisheye dewarp built in.

Genetec Video Player, Windows PC: download the 1.3 GB installer, open the
file, File, Save as, MP4. Greyed out unless the exporter allowed re-export. A
multi-camera `.g64x` is re-exported one sequence at a time, and Genetec's own
documentation says MP4/ASF output might lose supporting information.

VLC 4, when it ships: open the `.g64`, Media, Convert/Save, MP4. Would be the
easiest of all for playback, but today it exists only as source. Nothing
runnable was published on the nightly-build server when checked, and a
`.g64x` would have to be unzipped first.

## The honest caveats about g64conv

First published 2026-09-07. No PyPI release, so it installs from a git URL.
ffmpeg has to be installed separately. Encrypted exports (SRTP frames, or the
v5.32 password-protected header) still need Genetec software and the password.
HEVC archives are refused until a sample is available to verify against.

## Sources

Every link below was fetched on 2026-09-07 and returned the page.

- Genetec, [FAQ about Genetec Video Player](https://techdocs.genetec.com/r/en-US/FAQ-about-GenetecTM-Video-Player)
- Genetec, [Re-exporting G64 and G64x video files (5.14)](https://techdocs.genetec.com/r/en-US/Security-Center-User-Guide-5.14/Re-exporting-G64-and-G64x-video-files)
- Genetec, [Video Player commands (5.12)](https://techdocs.genetec.com/r/en-US/GenetecTM-Video-Player-Quick-Start-Guide-5.12/GenetecTM-Video-Player-commands)
- Genetec, [Exporting video in G64, ASF, and MP4 formats (5.12)](https://techdocs.genetec.com/r/en-US/Security-Center-User-Guide-5.12/Exporting-video-in-G64-ASF-and-MP4-formats)
- Genetec, [Converting video files to ASF or MP4 (5.12)](https://techdocs.genetec.com/r/en-US/Security-Center-User-Guide-5.12/Converting-video-files-to-ASF-or-MP4-format)
- VideoLAN, [`modules/demux/g64rtp.c` on master](https://github.com/videolan/vlc/blob/master/modules/demux/g64rtp.c) (added 2023-06-05, commit `08630bfb`; the same path is a 404 on the `3.0.x` branch)
- VideoLAN, [latest macOS build](https://get.videolan.org/vlc/last/macosx/) (3.0.23 when checked)
- GitHub, [repository search "g64 genetec"](https://github.com/search?q=g64+genetec&type=repositories) (1 result)
- Axon, [Axon Evidence third-party video: supported file types](https://www.axon.com/help/axon-evidence/software/axon-evidence/evidence/third-party-video/supported-file-types.htm)
- Amped Software, [Amped FIVE update 40823](https://blog.ampedsoftware.com/2026/05/20/amped-five-update-40823)
- Reduct, [Proprietary formats](https://help.reduct.video/en/articles/proprietary-formats)
- Garrett Discovery, [Can't play the video given to you in discovery?](https://www.garrettdiscovery.com/cant-play-the-video-given-to-you-in-discovery/)
- Video Production Stack Exchange, [Play a .g64 file extension](https://video.stackexchange.com/questions/15947/play-a-g64-file-extension) (2015 question, 2020 answer: no library or unofficial player found; the site answers 403 to non-browser fetches)

Not tested: VLC's demuxer on a real `.g64`, because no binary was available.
Its `.g64x` behaviour is inferred from the source, which contains no zip
handling, not measured.
