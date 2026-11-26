# Hyperframes Composition Brief: Daedalus (high motion cut)

## Output
- Composition: `brag-output/composition/`
- Video: `brag-output/brag.mp4`
- Landscape 1920x1080, 30 seconds

## Copy (unchanged, plain English, no em dashes)
- "Just tell Daedalus" / "what you need."
- "You send a message." / "I do the work."
- "what's on my calendar tomorrow?"
- "Eight named jobs. One loop."
- Eight names with one line jobs, lowercase dave
- "I remember." / "I keep your calendar." / "I show up on time."
- "Every action gets checked." / ALLOW / "You can see why."
- CONTEXT / MODEL SUGGESTS / PERMISSION CHECK / LOCAL ACTION
- "Nothing private leaves your laptop."
- "DAEDALUS" / "Your laptop. Your data. One message." / "Run it on your own laptop."

## Direction
- Fully Geist Sans. No serif, no hairlines, no borders, no small labels, no gradients, no glows.
- Serious and cool, high motion, slightly more color.
- Paper #f6f5f2, card #edece7, ink #1b1b1f, muted #55585f
- Teal #0f6b5c primary, blue #1a6491 and amber #a35b17 supporting
- Light tints #e4efeb, #e6eef4, #f5ece0 for selected panels

## Motion contract
- Text enters through masks with xPercent or yPercent and a short hold.
- Group entries stagger. Entries and exits 0.2 to 0.4 seconds.
- One distinct scene transition per cut: push left, slide up, bottom wipe, slide right, top wipe, diagonal, scale.
- All motion on the seekable GSAP timeline. No CSS transforms on animated elements.

## Scene timings
1. 0.0 to 3.6 hook
2. 3.6 to 7.2 message
3. 7.2 to 12.2 jobs
4. 12.2 to 16.6 memory
5. 16.6 to 22.0 checks
6. 22.0 to 24.8 privacy
7. 24.8 to 30.0 name and call to action

## Audio
- Seven local Kokoro voice files, am_michael, starts 0.8, 4.0, 7.6, 12.6, 17.0, 22.4, 25.2
- Music bed 30 seconds, ducked under voice, back up for final hold
- Soft interface clicks only. No external key, no spend.

## Requirements
- Faster motion without losing readability. Every word holds long enough to read.
- All text readable, WCAG AA through `hyperframes check`.
- Run `hyperframes check` before render.
