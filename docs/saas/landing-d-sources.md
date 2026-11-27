# Direction D footage: sources and licences

Everything in this folder comes from one public NASA video, cut by this product's own pipeline
(`app.clipper.standalone.clip_to_directory`). Checked 2026-11-28.

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

## Shipped files

**None yet (status 2026-11-28).** The licence check, source choice and edit list are done; no clip has been cut.
The cut runs on GitHub Actions, never on the owner's laptop: `.github/workflows/showcase.yml` downloads the NASA
original, checks its sha256, rebuilds the working copy from `scripts/showcase/edit_decision_list.json`, verifies it,
runs one cutting run and uploads the clips and judging evidence as the artifact `showcase-black_holes`. Kept clips,
their source ranges and the credits each contains are listed here once they are judged.
