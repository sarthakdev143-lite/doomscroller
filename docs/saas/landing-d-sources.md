# Direction D footage: sources and licences

The direction D footage comes from two public NASA videos, cut by this product's own pipeline
(`app.clipper.standalone.clip_to_directory`) on GitHub Actions (`.github/workflows/showcase.yml`, driven by
`scripts/showcase/edit_decision_list.json`), never on the owner's laptop. Checked 2026-11-28.

- Source 1, "Shedding Light on Black Holes" (`black_holes`): the sections below through "Third-party material
  inside the footage", and "Black Holes: cut results".
- Source 2, "Eye on Neptune" (`eye_on_neptune`): "Source 2: Eye on Neptune" (vetting evidence, edits, credits).
- The NASA licence decision ("Licence decision") covers both.

## Source

| | |
|---|---|
| Title | NASA ScienceCasts: Shedding Light on Black Holes |
| Publisher | NASA Science Mission Directorate (Science@NASA), NASA Headquarters |
| Released | 2019-08-13 |
| Library page | https://images.nasa.gov/details/301_BlackHoles (standard library URL; not opened in a browser. The item was read through https://images-api.nasa.gov/asset/301_BlackHoles and its metadata.json) |
| File used | https://images-assets.nasa.gov/video/301_BlackHoles/301_BlackHoles~orig.mp4 (1920x1076, 246.74 s) |
| Captions (reference only) | https://images-assets.nasa.gov/video/301_BlackHoles/301_BlackHoles.srt |
| Narrator | Not credited anywhere on the library page or in the video. No one appears on camera in the parts used. |
| Music | None under the narration. Measured: 52 gaps that fall to digital silence (-60 dB) between sentences, evenly spread through the narration. The only music is the Science@NASA intro sting (0-13.9 s), which is cut. |

### Why this video (and not another NASA one)

NASA's own guidelines (below) allow advertising use of its footage but forbid current NASA employees'
"names, likenesses or other personality traits" and the NASA insignia in advertisements. Nearly every
narrated NASA production (Goddard/SVS) also carries a licensed music bed (e.g. SVS 4619 "Tour of the Moon
4K Redux": "Music Provided By Killer Tracks"), which NASA cannot pass on ("NASA's use does not convey any rights
to others"). Six Goddard candidates tested had continuous audio under the narration (1-6 silent gaps in 3-6
minutes). The 2019 ScienceCasts are narration-only; this one has the strongest visuals and hooks.

ESA/Hubble and ESA/Webb were ruled out as sources: their terms exclude music from CC BY ("music ... on this site
[is] not released under the Creative Commons Attribution 4.0 International license"). ESO's own terms are
inconsistent on music.

## What was changed before cutting (working copy)

The working copy (125.05 s, built in the showcase workflow from `scripts/showcase/edit_decision_list.json`, never
committed) is the original with these edits only. Every cut sits in a pause between sentences.

| Original time | Treatment | Why |
|---|---|---|
| whole video | crop 1642x924 at (264,152) | removes the persistent "science @ NASA" insignia bug (top right) and the Event Horizon Telescope logo (top left on EHT frames); keeps the image credit lines |
| 0.0-20.1 s | cut | Science@NASA bumper (NASA wordmark) and screenshots of science.nasa.gov (NASA insignia, a tweet with a group photo); their crossfade ends at 20.033 s |
| 84.2-138.6 s | cut (myth 2) | an artwork credited "Robin Dienel, Carnegie Institution for Science" whose licence could not be verified; and narration naming and quoting Dr. Dan Evans (NASA Headquarters astrophysicist) over his photo card |
| 174.3-203.95 s | cut (myth 4) | Stephen Hawking's photo and name card (estate rights) appear before the sentence ends |
| 229.2-246.74 s | cut | "visit science.nasa.gov" and the NASA end card |

Working time to original time: `orig = work + 20.1` (work < 64.1), `work + 74.5` (64.1-99.8), `work + 104.15` (>= 99.8).

## Licence decision

**NASA material: usable in advertising, with conditions we meet.** Quoted from NASA's pages, fetched 2026-11-28.

NASA Advertising Guidelines (https://www.nasa.gov/nasa-brand-center/advertising-guidelines/, page last updated Mar 31, 2025):

> "Many NASA images and selections of film and video footage produced and publicly released by NASA may be used
> for advertising purposes. However, there are rules regarding the appearance of NASA astronauts' or NASA
> employees' names, likenesses or other personality traits in advertising materials. Astronauts or employees who
> are currently employed by NASA cannot have their names, likenesses or other personality traits displayed in any
> advertisements or marketing material."

> "NASA does not permit use of the NASA Insignia and other NASA indicia in advertisements."

> "As a U.S. government agency, NASA will not promote or endorse or appear to promote or endorse a commercial
> product, service or activity."

NASA Images and Media guidelines (https://www.nasa.gov/nasa-brand-center/images-and-media/, page last updated Oct 27, 2026):

> "NASA content - images, audio, video, and media files ... generally are not subject to copyright in the United
> States."

> "If the NASA material is to be used for commercial purposes, including advertisements, it must not explicitly or
> implicitly convey NASA's endorsement of commercial goods or services."

> "If a NASA image, audio, video or media includes an identifiable person, using the media for commercial purposes
> may infringe that person's right of privacy or publicity, and permission should be obtained from the person."

> "NASA occasionally uses copyright-protected material of third parties with permission on its website. ...
> NASA's use does not convey any rights to others to use the same material."

> "NASA should be acknowledged as the source of the material."

The same page's COMMERCIAL USE section also says:

> "For use of NASA images clearances may be necessary for images that include any NASA logos or NASA employees to be
> used as cover art or in promotional content.  Otherwise, NASA imagery can be generally used editorially within
> published works that are not promotional in nature."

Read alone, that sentence only blesses editorial use. It is not the governing clause for us: the Advertising
Guidelines quoted above address promotional use directly ("Many NASA images and selections of film and video
footage ... may be used for advertising purposes") and set the conditions (no current employees' names, likenesses
or personality traits; no insignia; no endorsement). The clearance it mentions is for logos and NASA employees,
which the working copy removes.

Conditions every shipped file must pass (checked per file when the clips are judged):
- no identifiable person on screen (astronaut footage is not used at all);
- no NASA insignia or wordmark in any frame (the bug is cropped; bumpers and the end card are cut);
- no title, hook or caption that says "NASA";
- the page credits NASA as the source of the footage (footer or beside the media) and nothing more: no "as used by
  NASA", no NASA logo, nothing that implies endorsement.

Residual risk: the narrator's voice. They are not named in the video or on the library page, but a voice is a
"personality trait" if the person is identifiable. Low risk; listed for the owner.

**Owner decision before go-live.** The Advertising Guidelines also say:

> "Companies interested in producing NASA-related advertisements must notify the Multimedia Division of NASA's
> Office of Communications. Requests should describe the intended use of NASA imagery in the ads. ... Requests
> regarding NASA related advertisements must be made in writing by sending an e-mail to
> nasa-merchandise-&-brand-team@mail.nasa.gov."

The page is not about NASA, but it does use NASA footage in marketing. Whether to send that notice is the owner's call.

## Third-party material inside the footage

NASA's video includes images from other organisations, each credited on screen. Their licences (checked at the
origin) require the credit to be shown visibly and unaltered. The in-frame credits survive the crop but are small in
a Short, so the page must print the lines below beside (or directly under) the footage that contains them.
`manifest.json` lists which credits each shipped file contains.

| Working time (s, approx.; measured on an earlier cut, re-check on the rebuilt copy) | On-screen credit | Licence | Checked at |
|---|---|---|---|
| 0-2, 4-6 | NASA/JPL-Caltech - Artist Rendering | NASA/JPL: no copyright, NASA guidelines | NASA guidelines above |
| 2-4 | NASA/NRAO/AUI/NSF - Artist Rendering | NRAO: "All NRAO produced images and videos on this site are copyright protected on behalf of ... (NRAO), (AUI), and (NSF), and are licensed for use under the Creative Commons Attribution 4.0 Unported license"; "The image or video credit ... must be clearly visible". Its narration restriction covers NRAO's own narrated videos, not an image inside NASA's. | https://public.nrao.edu/media-resources/ |
| 6-8 | NASA - Artist Rendering | NASA | |
| 7-9 | ESO/WFI (Optical); MPIfR/ESO/APEX/A.Weiss et al. (Submillimetre); NASA/CXC/CfA/R.Kraft et al. (X-ray) | ESO: CC BY 4.0 ("Unless specifically noted, the images, videos, and music distributed on the public ESO website ... are licensed under a Creative Commons Attribution 4.0 International License"); Chandra: "no claim to copyright is being asserted" | https://www.eso.org/public/images/eso0903a/, https://chandra.harvard.edu/photo/image_use.html |
| 9-16 | Computer Simulation, Credit: NASA, ESA, D. Coe, J. Anderson, R. van der Marel (STScI) | NASA/ESA Hubble release (NGC 1600, 2016): NASA public domain; ESA/Hubble CC BY 4.0 | science.nasa.gov/image-detail/behemoth_blackhole/ |
| 26-40, 101.6-125 | Credit: Event Horizon Telescope Collaboration - 2019 | CC BY 4.0, published by ESO as eso1907a, credit "EHT Collaboration", no exception noted | https://www.eso.org/public/images/eso1907a/ |
| 42-49, 58-63 | NASA/GSFC - Artist Rendering; NASA/GSFC Conceptual Image Lab | NASA | |
| 50-57 | NASA/Swift/Aurore Simonnet, Sonoma State Univ. - Artist Rendering | NASA (NASA education and public outreach artwork, credited to NASA) | |
| 77.6-98.6 | Artist Rendering - Not to scale | NASA | |

ESO's terms also say: "ESO materials, images and videos may not be used to state or imply the endorsement by ESO or
any ESO employee of a commercial product or service" and "If an image includes a picture of an identifiable person,
using that image for commercial purposes is not permitted." Neither applies here.

## Black Holes: cut results

Run 46085200172 (success): 3 clips returned for 8 asked.

| Clip | Title | Decision | Why |
|---|---|---|---|
| `clip_01_20_50` | Myth Busted: Black Holes Emit Light | **Keep** | Title and hook match myth 1. The 0.37 s dark stretch at 16 s is NASA's own dissolve, not a render fault. |
| `clip_02_64_85` | How Close Can You Get to a Black Hole? | **Keep** | Matches myth 3. Note: it opens on the spoken "Myth 3" (and its title card), which a page caption may want to account for. |
| `clip_03_86_117` | (not shipped) | **Reject** | Ends mid-sentence ("…learn even more about the properties"); drifts from swap-the-sun into the EHT/Einstein lines and NASA's outro; 0.9 s of black at 10.1 s. The mid-sentence end is being investigated separately in `app/clipper/`. |

The burned-in caption detector said **True** (the myth title cards print the spoken sentence); the driver override
kept our word captions, as the edit list documents.

`cut_on_push` is now **false** for this source, so later pushes do not re-cut it.

## The standard every shipped frame meets

- No identifiable person (no faces, no name cards; astronaut footage is not used at all).
- No NASA insignia, wordmark or logo in any frame; no title, hook or caption that says "NASA".
- No third-party material unless its licence allows commercial use, checked at the origin, with its credit kept.
- No licensed music: narration only, measured (a narration-only track falls silent between sentences).
- No dated or housekeeping lines ("on September 9", "visit science.nasa.gov").
- Every cut sits in a pause between sentences; nothing of a removed segment's picture leaks in (bumpers, dissolves).

## Source 2: "NASA ScienceCasts: Eye on Neptune" (`eye_on_neptune`)

Vetted on 2026-11-28 from the workflow's vetting pack (contact sheets every 2 s, top-corner strips every 4 s,
bottom credit band every 2 s, frames at every cut point, silence profile, caption wording scan), plus five single
full-resolution frames fetched by range request to test the bug treatment.

| | |
|---|---|
| Original | https://images-assets.nasa.gov/video/302_EyeOnNeptune/302_EyeOnNeptune~orig.mp4 (NASA HQ, Sept 2019) |
| Size / sha256 | 92,878,849 bytes / `63e01601b2964438a9a7650fdeb87bbaf3f69699aa9e87c2f9cb8cc9c4753e86` (pinned) |
| Streams | H.264 1900x1068 30 fps; AAC stereo 48 kHz (track used) + AC-3; 194.9 s |
| Verdict | **Passes, as a 59.37 s working copy of three segments.** `approved: true`, `cut_on_push: true` |

### Kept segments (original seconds) and why each boundary is where it is

| Segment | Keep | Says | Boundary evidence |
|---|---|---|---|
| 1 | 14.40-21.40 | "It is an ice giant, a planet of monster storms, and a recluse rarely seen." | Speech 14.76-21.07 (-60 dB pauses 13.85-14.76, 21.07-21.73). The Science@NASA wordmark is still faintly visible at 14.19 and gone by 14.29. "September 9th" is drawn on screen from ~22 s. |
| 2 | 98.49-128.90 | "Voyager 2 provided our closest glimpse of Neptune … a dark and violent vortex … Since then, observatories such as the Hubble Space Telescope have given us additional observations of the planet from afar." | Speech 98.91-128.84 (pauses 98.06-98.91, 128.84-129.74). The preceding "…in 1989." ends at 98.06. The Hubble-to-Neptune dissolve starts ~129.0. |
| 3 | 139.44-161.40 | "Scientists have determined that Neptune has seasons … spanning decades instead of months … including right here on Earth." | Speech 139.86-161.34 (pauses 139.08-139.86, 161.34-162.00). The Earth shot dissolves into a Stellarium sky from ~161.6. |

The segments join as one coherent narration: "…a planet of monster storms, and a recluse rarely seen. Voyager 2
provided our closest glimpse … from afar. Scientists have determined that Neptune has seasons …".

Working time to original time: `orig = work + 14.40` (work < 7.0), `work + 91.49` (7.0-37.41), `work + 102.03`
(>= 37.41).

### What was dropped, and why

| Original | Reason |
|---|---|
| 0-14.40 | Science@NASA bumper and wordmark (fades out 13.7-14.3). |
| 21.40-64.66 | Dated lines ("on the night of September 9 … at opposition", "evenings in September"); stargazer silhouettes (32-38 s) and a person at a computer (40-42 s); "September 9th" on screen. |
| 64.66-89.17 | Stellarium-rendered sky and sky chart ("Skymap Credit: Stellarium", 44-69.6 s) under a sentence that runs 65.08-80.57, so no sentence-boundary cut avoids it (see licences below). Without it, "That atmosphere surrounds…" (81.29-88.84) has no antecedent. Also ends in a dissolve into 1977 launch footage (~88.85). |
| 89.17-98.49 | "This opposition event also comes just after the 30th anniversary of the Voyager 2 spacecraft's flyby of the planet, in 1989." (dated); a "1989" title on screen. |
| 128.90-139.44 | "NASA scientists have learned, for instance, that unlike Jupiter, the Neptune vortex disappears for a time…" (says NASA). |
| 161.40-194.9 | Stellarium sky; "So consider taking a look at … Neptune in September" (dated); telescope silhouettes; "visit science.nasa.gov"; NASA end cards. |

### Insignia bug

A "science @ NASA" bug with the NASA insignia sits top right from 12 s to 2:56, at about x 1635-1840, y 55-142 of
1900x1068 (measured from the 0.658-scale corner strips; uncertainty about ±5 px). No single 16:9 window clears it:
a window below it is at most ~1624 px wide, and the kept shots carry credits or labels at both edges (left-edge
"Artist Rendering" and the 1996/1998/2002 year labels at x ~89-312; right-aligned credits to x ~1818). So each
segment gets the treatment its own frames allow, scaled to a common 1900x1068 (segments are scene cuts, so a
change of window at a join reads as an edit):

| Segment | Treatment | Evidence |
|---|---|---|
| 1 | crop 1618x910 at (0,158) | Dense starfield behind the bug, where `delogo` left a visible streaky block (frame at 17 s). The window keeps the planet (x ~742-1128) and the left credit. |
| 2 | crop 1618x910 at (282,158) | The Voyager photos pass under the bug at 1:50-1:58, where `delogo` would smear them. Frame at 113 s through this window: clean, no bug, photos intact. Keeps the right-aligned credits. |
| 3 | `delogo` x 1622, y 44, w 232, h 112 | Corner is black or sparse stars; the frame at 149 s showed no trace. The full frame keeps the year labels (left) and the Sromovsky/Fry credit (right). A faint trace is possible for ~0.5 s at the segment's start, where the previous shot's speckled starfield is dissolving out. |

The workflow's rebuild step uploads top-right and top-left corner strips of the built working copy (`checks/`);
those are the proof that no insignia survives.

### Credits in the kept ranges, and their licences

| Working time (approx.) | On-screen credit | Licence |
|---|---|---|
| 0-7.0, 7.0-14.5, 30.5-38 | Artist Rendering | NASA (no copyright in the US; see "Licence decision") |
| ~16.5-28.5 | (none) Voyager 2 photographs of Neptune | NASA/JPL. NASA marks third-party material "as copyright protected with the name of the copyright holder"; these are unmarked. |
| ~38-46.5 | Image Credit: NASA, L. Sromovsky, and P. Fry (University of Wisconsin-Madison) | NASA Hubble release "Brighter Neptune Suggests a Planetary Change of Seasons" (May 15, 2003), hosted by NASA at https://science.nasa.gov/missions/hubble/brighter-neptune-suggests-a-planetary-change-of-seasons/ with credit "NASA, L. Sromovsky, and P. Fry (University of Wisconsin-Madison)" and no copyright notice. HubbleSite's copyright page (https://hubblesite.org/copyright) now redirects to NASA's Images and Media guidelines. Treated as NASA material; keep the credit line. |
| ~46.5-55 | Voyager 2 Images Time-Lapse | NASA/JPL |
| ~56-59.4 | (none) full-disk Earth | NASA (unmarked) |

**Stellarium (why segment 2 of the original plan was dropped).** Stellarium's FAQ
(https://github.com/Stellarium/stellarium/wiki/FAQ), on "Can I use Stellarium screenshots or videos in my
book/website/blog/vlog/calendar/almanac etc?": "If you want to use screenshots or video sequences for publications,
videos, presentations etc., this is fine in principle for non-profit productions (or those released under
CreativeCommons terms) if credit is given. However, some of the images and textures used by Stellarium, and
especially the skyculture data, are copyright by others, [and] may require separate attribution or copyright/use
clarification with the original authors." A commercial landing page is neither non-profit nor CC-released, so
Stellarium frames are out.

The working-copy times above are measured from the original's credit bands; re-read them from the job's
`checks/credit_band_*.jpg` strips of the built copy before printing credits on the page.

### People, dates, NASA wording, music

- People: none in the kept ranges (Neptune renders, Voyager renders and photographs, Hubble images, a Hubble
  rendering over Earth, a full-disk Earth). Every person in the video (silhouettes at 32-38 s and 2:52-2:56, a person
  at a computer at 40-42 s) is in a dropped range.
- Dated lines: none kept. The vetting scan flagged only "in 1989", whose cue overlaps segment 2's start in the SRT
  timing, but the spoken words end at 98.06, before the 98.49 cut. The on-screen year labels 1996/1998/2002 are
  observation dates on historical images, not time-sensitive copy.
- "NASA" spoken or captioned: none kept.
- Music: narration only. Every 15 s window of every kept segment holds a pause at -50 dB; 39 pauses at -60 dB
  across the video.
- Caption detector: the source has no burned-in speech captions in the kept ranges (only credits and year labels),
  so the driver keeps our captions if the detector misfires; its real verdict is logged.

## Open items

- **Credit times on the built copies.** Re-read both sources' credits from the job's `checks/credit_band_*.jpg`
  before the page prints them; list each shipped clip's credits in this file.
- **Caption-detector override** is on for both sources (Black Holes: title cards; Neptune: precaution). Keep it
  in the record and in the report to the owner.
- **NASA advertising notice.** Whether to email nasa-merchandise-&-brand-team@mail.nasa.gov before go-live is the
  owner's call (clause quoted under "Licence decision").

- **Neptune's treatment is proven only on single frames.** The `delogo` trace at segment 3's first ~0.5 s and the
  crop windows must be checked on the built copy's corner strips and the clips' contact sheets.
- **Narrator's voice** (uncredited in both videos): a low residual risk, recorded under "Licence decision".
- Web encodes, posters, the hero excerpt, the high-quality excerpt and `manifest.json` are not built yet.
