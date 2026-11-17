# #4 chore(deps): update dependency hypfer/valetudo to v2026.07.0

- Kind: pull, state: closed, by cody, created 2026-07-23T12:10:46-07:00
- Labels: none; milestone: none
- refs/pull/4/head -> main, merged: True

This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| [Hypfer/Valetudo](https://github.com/Hypfer/Valetudo) | minor | `2026.05.0` → `2026.07.0` |

---

### Release Notes

<details>
<summary>Hypfer/Valetudo (Hypfer/Valetudo)</summary>

### [`v2026.07.0`](https://github.com/Hypfer/Valetudo/releases/tag/2026.07.0): Valetudo 2026.07.0

[Compare Source](https://github.com/Hypfer/Valetudo/compare/2026.05.0...2026.07.0)

<div align="center">
    <img src="https://raw.githubusercontent.com/Hypfer/Valetudo/master/assets/logo/valetudo_logo_with_name.svg" width="800" alt="valetudo">
    <p align="center"><h2>2026.07.0</h2></p>
</div>

Map annotations, carpeted rooms and updated moderation

### Valetudo 2026.07.0

I've been using some downtime to clean up my shelf, which is basically the entirety of this release.

#### Map Annotations

Map annotations are the generic concept to house any kind of "there is something on the map the robot needs to be aware of, which might not be reliably auto-determined".
For example, depending on firmware support, that could be a passable threshold, a curtain, a ramp or similar.

<img src="https://github.com/user-attachments/assets/57cd25f7-d279-4707-b743-18e4dee7e352" />

These things existing in the map make the firmware behave differently in some way relating to the location they're at.
It might then take a specific path or trust its obstacle avoidance less or really anything like that.

This feature took me quite a while, not because the code would've been complicated, but because the concept should be easily adaptable to the future. At least as adaptable as something can reasonably be while trying to predict the future.

I again had fun designing some more custom icons, but I can't tell you more about that, because that happened 6-8 months ago and my memory of it is by now a bit fuzzy.

<img src="https://github.com/user-attachments/assets/7ffe78fb-f429-447d-b172-87494355554a" />

In said predicted future, there might be more map annotations. Though, as always and consequently, I will refrain from just mirroring the vendor app verbatim.

For example, Dreame has "non-passable thresholds" which, to my understanding, logically achieve the exact same as virtual walls.
I can see why for UX they have them as a counterpart that exists in the same view, but also, for UX reasons, it's kinda dumb to have two different things that do exactly the same.
.. or I am just missing something, but I did at least ask the support and they didn't know either.

##### What do we have?

For now, on Dreame, starting with the X/L40 Master/Ultra, we have thresholds, curtains and ramps.
On older Dreames going back to the L10s Ultra, we only have thresholds.

Unfortunately, I have none of those things at home, so I cannot personally test those things much.
I've already asked users to test the nightly build and heard back good results for thresholds and curtains, but ramps are still a big mystery.

So please, let me know if those ramps (and the other annotations) work for you.

What they *should* do is:

1. For thresholds, the robot should be able to better climb them through being aware that they exist and driving accordingly
2. For curtains, the robot should be able to clean below them through ignoring the "THERE IS A SOLID WALL" info it receives from its Lidar
3. For ramps, the robot should take the designated path to reach an otherwise inaccessible area

<https://github.com/user-attachments/assets/162c5323-2de5-43d4-acb6-d286f980b177>

I also have the Midea implementation here, however, that has not seen any testing so far, so for this release, it's only Dreame.
Also, no ramps there.

One thing I am not quite happy with yet are the visuals in the map. Especially the colors, but honestly everything.
So if you have thoughts/ideas there, please let me know!

The only thing that will for sure stay is the wavieness of the curtain. A lot of tuning went into that.

##### Virtual Restrictions UI Cleanup

Since I had to implement this grouped button for the Map Annotations anyway, I've also updated the Virtual Restrictions UI to use it:

<img src="https://github.com/user-attachments/assets/f9298c23-9ddd-4689-b545-4e66ac6b8806" />

Much better. Still two rows, but not three.

#### Segment Floor Material Type Carpet

Staring at firmwares, I've noticed that Dreame has, for probably a few fw versions, supported setting the floor material of a segment to carpet. The existence of fully carpeted rooms has finally been acknowledged by big tile.

So now you can do that (if supported by your robot).

<img src="https://github.com/user-attachments/assets/9f8bb38e-7049-4aa5-920c-ebdae5fa72f4" />

There are up to three types of carpet floor material. low-pile, high-pile and just carpet.
I think the visualization turned out quite well.

Additionally, I've once again looked into why Dreame bots seem to not want to remember the segment material.
I found another possible culprit, and, in my testing, it didn't happen anymore, however, you tell me.

Fingers crossed (again)

#### New Dreame Firmwares

As announced in [dust\_announce](https://t.me/dust_announce), new firmwares for the Dreame X/L40 Ultra/Master + the MOVA P10 Pro Ultra have been added to the dustbuilder.
They come with an update to the obstacle avoidance model as well as probably logic changes.

#### What else is going on?

As with the last release, most of the meat is within this section.

##### Rooting all the things

Starting off with something non-Valetudo, I got a rootshell on my 2018 Bosch EBike's Nyon display:

<img src="https://github.com/user-attachments/assets/c4ee6a71-6d89-44bc-83c7-321b10a02f68" />

This now exposes an API with data that should've been there in the stock firmware

<img src="https://github.com/user-attachments/assets/18b57fb9-b994-4d46-b1ec-8da6af93cc28" />

So that's kinda cool and completely not ready to be shipped.

Now that I'm there, it's not all that difficult to pull off, but the way up to there took a while.

Anyway, I'm not exactly sure where to go with this. Lots of questions still to be asked and answered.
But be advised: It's possible.

GLM-5.2 has been super useful there. Cool model.
Can't wait to someday run this class of LLM at home on my own hardware.

##### Valetudo Moderation

The Telegram moderation bot [Easterwave](https://github.com/hypfer/easterwave) received some love now that Telegram allows for sending ephemeral messages that are only visible to a specific user. This way, the bot can now provide feedback to a user, which is the first component of a feedback loop.

Additionally, I've added some further regex patterns, which should reduce the amount of people I need to manually ban.

Combined, this might make people hate me less, because suddenly they have some feedback + it was a machine that did it.
But also, and more importantly, less interrupts for me through stuff that is incorrect (rel: XKCD 386) and with that more unfragmented focus time.

##### On solving social problems with technical solutions

This is actually a fascinating topic, because common wisdom would tell you that you cannot solve a social problem with a technical solution. And that is true, but, interestingly, consumerist entitlement is so devoid of humanity and so formulaic and script-based that it actually starts being trivial to be countered with mere regexes.

This makes a lot of sense, because corporate processes are standardized to conserve energy and be optimized.
So, consequently, the bypasses to them would also be standardized to conserve energy and be optimized.
And, consequently to that, the bypass blocker can equally be standardized (and written as a simple regular expression) to conserve energy and be optimized.

For example, let us consider this regex:
`/is there (?:a |any |some )?(?:particular )?reason/i`

It is beautiful, because, within the context of FOSS like this, it basically exclusively triggers on bad faith.
That arrangement of words is inherently unnatural, stilted and inauthentic, because it is an engineered wrapper to bypass corporate HR firewalls.

The actual message there is "I don't like this." or "I want something." or "I believe it should be different", but those real statements would be shunned when said like that. So instead, it gets wrapped into this "curious inquiry" standard issue wrapper.

> "Dear sir, I was just wondering why you did this."
> \[...]
> "I am outraged that you would act with this kind of hostility to a mere question"

You get the idea. Something something sea lion.

The purpose of the wrapper is to make any action other than compliance be an illegal move.
And, if you as a FOSS maintainer (or really person in general) play by those rules, you either become a martyr or persona non grata.

If you take a peek at the code, you will find a bunch more of them. You might find that interesting.
You might also occasionally find yourself using the same scripts. I do, at least. It is - after all - an environmental poison.

Which is also why some of those regexes will fire even though people aren't being actively malicious.
We've just all absorbed the inherent maliciousness of our environment and, unfortunately, sometimes reproduce it without noticing.

Another good reason for having a bot deal with it instead of another human.

##### Positive outlook

To wrap this side-track up and go from discussing dont's to proposing do's, let's look at how to not trigger that mentioned example regex.

At its core, good faith communication would not try to hide the true motive but run with it.
For example, a phrasing like the following would work better:

> "I expected the thing to work like this, but instead it works like that. Now I'm wondering why that is"

As you can see, it succeeds through putting all the cards on the table right away, instead of trying to hold some back for later leverage.
It approaches the other party with respect and at eye level, telling them exactly where one is coming from, what drives them, and what their true goals are.

This of course requires the goals to not be ones that would be shunned by society if put plainly, but that is the whole idea behind this exercise.

<br/>

Anyway, for some reason, I regularly forget that I can just use software to solve issues I have, just to then rediscover that possibility and be full of joy. Not sure why. A very weird loop.

But also an important reminder that the game of social interactions is rigged against my people and playing by the rules will just get you into a bad place. Hence, you need to hack it, automate it and conserve energy for the things that matter.

I am curious what the future will bring there. Imagine your rooted cyberpunk implant running a bullshit filter in real time.
Possibilities are endless and just limited by the fact that there are no such implants and this is all a pipe dream.

But one can dream.
How would we even solve latency?

##### Some cool use of LLMs

I have learned through extensive exhaustion that my ramblings aren't always easy to decode for people.
With the advent of LLMs though, I have found a great solution to that. I can use them as some kind of translator-ish machine.
A translator that sometimes doesn't work at all - certainly not on autopilot - and if it does, it's still a lossy translation.

I have so far rather successfully used this tooling in a feedback loop to extract what makes its communication work and improve my own mental model, and, eventually, reduce my token spending.
But, there is still some way to go, so, for your convenience, if that "the game" thing was a bit too obscure, Gemini did (I think) a good enough job at decoding my ramblings:

> At its core, "The Game" is an unwritten, continuous negotiation over who has to spend cognitive and emotional energy.
> If we look at human social interaction as a distributed system, The Game is the protocol normal society uses to resolve conflicts, establish hierarchy, and allocate resources (the resource being attention and effort).

You all know this (possibly subconsciously), but not all of us are equally well equipped at birth to actually run this in hardware at scale.
This is.. not ideal, because it presents this arbitrage opportunity, where great amounts of value can be extracted at a bargain through various known patterns such as exhaustion attacks, traps or just general force.

Anyway, cool prosthetics. The bot, but also LLMs.
Don't let your pride (or "beauty standards") stop you from using one yourself if you should require one.
And don't just sign up to a (metaphorical) subscription for it, please.

##### One more thing

That management book I found in a public bookshelf while on vacation told me that you should end with "one more thing" because Steve Jobs did it. So.. one more thing:

Expect new robots in the hopefully near-ish future.
We've been finishing up most of that, but there are currently some infrastructure topics blocking the finalization.

We're now looking at 8GB eMMCs, which doesn't exactly bode well for the current dustbuilder deployment, given that everything is much larger.
So yeah, this dumb shit is what slows you down. As always.

#### The usual

If you want to see Valetudo on more robots and/or like this release, you might want to consider donating:

<https://github.com/sponsors/Hypfer>

<https://builder.dontvacuum.me/donations.txt>

#### Autogenerated changelog

##### Features

- **vendor.dreame**: MapAnnotationsCapability [`58446b8`](http://github.com/Hypfer/Valetudo/commit/58446b868ddf466a25173e5d6c0390f13fe749a4)
- **ui**: MapAnnotationsCapability [`49bee99`](http://github.com/Hypfer/Valetudo/commit/49bee992f0765f814937e6fe0a9ceffca02c2dce)
- **core**: MapAnnotationsCapability [`f1db08b`](http://github.com/Hypfer/Valetudo/commit/f1db08b8cb7d482a567db83a16c60e63b4863d3f)
- **ui**: Reduce virtual restriction edit map button count using a unified add button [`80a3c25`](http://github.com/Hypfer/Valetudo/commit/80a3c25eb412e332a898c37748de6f95d864e2ea)
- **vendor.dreame**: Segment material type carpet [`a975c3a`](http://github.com/Hypfer/Valetudo/commit/a975c3afef23a31f9e73a1e0f418283bc05dbf9b)
- **vendor.dreame**: Map two more error codes [`6889856`](http://github.com/Hypfer/Valetudo/commit/688985601f3b0fb5bad1e047c166dc4f0fa7f625)
- **core**: Segment material type carpet [`cdd6710`](http://github.com/Hypfer/Valetudo/commit/cdd6710c82e481ac1ba16e1377ff8c28fc7d4d85)
- **vendor.dreame**: Add fan speed turbo quirk [`b4e3326`](http://github.com/Hypfer/Valetudo/commit/b4e3326070329d1147b4c569295601c697fc803d)
- **vendor.midea**: Various no-up handlers for stuff in J15Max FW 558 [`72226e9`](http://github.com/Hypfer/Valetudo/commit/72226e92c512dc6b5eb72c89ec36129b9ca90717)

##### Fixes

- **vendor.dreame**: Pass the mapId when saving segment material to maybe fix reverting material settings [`e18dfcd`](http://github.com/Hypfer/Valetudo/commit/e18dfcd7edcb2c6e5716718877d1977cc7e011ce)
- **ui**: Some client\_structure cleanups [`345dab4`](http://github.com/Hypfer/Valetudo/commit/345dab497392b4156eb9ed8e1d28c5a9454ced24)
- **vendor.dreame**: Fix Deep carpet cleaning quirk gracefully handling the firmware saying no [`371812c`](http://github.com/Hypfer/Valetudo/commit/371812c03728c6087bcd7986b180e2d5079ffebd)
- End logWriteStream instead of closing it [`df740d9`](http://github.com/Hypfer/Valetudo/commit/df740d9e513051d34ae633b332887bda81801e9d)

##### Chores

- **release**: 2026.07.0 [`bffad62`](http://github.com/Hypfer/Valetudo/commit/bffad6285280a5a41953cd27296b7e3adbdde0c7)
- **vendor.viomi**: Clean up unnecessary optional chaining [`db19ec1`](http://github.com/Hypfer/Valetudo/commit/db19ec1da3e45d4d2d4c56b12310b395bc7664a8)
- Minor cleanup [`fc9df01`](http://github.com/Hypfer/Valetudo/commit/fc9df01485b40047efab3a0492711c428aa9c486)
- Some cleanup [`f14b4c0`](http://github.com/Hypfer/Valetudo/commit/f14b4c077ef94f8e5f5716e768ffea857daba8aa)
- **test**: Switch to native nodejs test runner [`10866d0`](http://github.com/Hypfer/Valetudo/commit/10866d00e323a35b85869b8c7f3c96abc39793ae)

</details>

---

### Configuration

📅 **Schedule**: (UTC)

- Branch creation
  - At any time (no schedule defined)
- Automerge
  - At any time (no schedule defined)

🚦 **Automerge**: Disabled by config. Please merge this manually once you are satisfied.

♻ **Rebasing**: Whenever PR is behind base branch, or you tick the rebase/retry checkbox.

🔕 **Ignore**: Close this PR and you won't be reminded about this update again.

---

 - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box

---

This PR has been generated by [Mend Renovate](https://github.com/renovatebot/renovate).
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0My4yNzkuMCIsInVwZGF0ZWRJblZlciI6IjQzLjI3OS4wIiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJtaW5vci11cGRhdGUiXX0=-->
