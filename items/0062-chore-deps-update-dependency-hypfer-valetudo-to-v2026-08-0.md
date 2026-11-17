# #62 chore(deps): update dependency hypfer/valetudo to v2026.08.0

- Kind: pull, state: closed, by renovate, created 2026-08-03T05:03:14-07:00
- Labels: none; milestone: none
- refs/pull/62/head -> main, merged: True

This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| [Hypfer/Valetudo](https://github.com/Hypfer/Valetudo) | minor | `2026.07.0` → `2026.08.0` |

Update all `VALETUDO_SHA256` entries in `dreame_valetudo/constants.py` from the new release asset digests before merging.

---

### Release Notes

<details>
<summary>Hypfer/Valetudo (Hypfer/Valetudo)</summary>

### [`v2026.08.0`](https://github.com/Hypfer/Valetudo/releases/tag/2026.08.0): Valetudo 2026.08.0

[Compare Source](https://github.com/Hypfer/Valetudo/compare/2026.07.0...2026.08.0)

<div align="center">
    <img src="https://raw.githubusercontent.com/Hypfer/Valetudo/master/assets/logo/valetudo_logo_with_name.svg" width="800" alt="valetudo">
    <p align="center"><h2>2026.08.0</h2></p>
</div>

Camera Streaming — Yes, really

### Valetudo 2026.08.0

I've put an em dash there as a jumpscare. If you didn't notice, I have bad news for you.
You might've previously been exposed to AI writing without you even noticing.

(These release notes are quite inline-video-heavy, so for the best experience, I recommend [reading them on github](https://github.com/Hypfer/Valetudo/releases/tag/2026.08.0).)

#### Duststreaming

<img src="https://github.com/user-attachments/assets/0bb3d094-ae88-416a-86f3-8051726453ce" />

##### Why (only) now?

So while this always was a technically complex thing, the main blocker has always been an ethics one.

Because the software works and because a good Valetudo user is a self-sufficient invisible one, in my daily grind, I - for the most part - do not get to interact with you cool people.
Instead, I get to experience the absolute worst consumer smarthome tech + foss has to offer, which historically made me quite hesitant to implement a technology with as much abuse potential as camera streaming on vacuum robots.

I did however eventually realize that letting this decision be guided by the most terrible of people in a sense means supporting these guys in making the world worse by stripping us of cool stuff.
So I sat down and found a solution.

The feature now exists, but it comes with safeguards and a lecture that strips away any sort of plausible deniability for bad behavior.
I cannot stop people from being shitty, but I think I’ve managed to ensure they'll at least feel bad about it when they are.

Additionally, I do believe that the messaging in there is genuinely useful and extends beyond just vacuum robots, but you tell me.
You can of course also just skip it by typing skip.

##### Design notes

This whole "designing for better results" doesn't stop there, though.

It extends to the feature being built with non-adoption as the goal.
When it is off, you will see no mention of it in the menus other than the toggle to turn it on.

This avoids the classic silicon valley thing of putting something disabled in front of you to make you feel like you're missing out - just so that you're to blame when you eventually do what they've funneled you into doing. With Valetudo, not having the spycam enabled is not just an option but the default people should hopefully gravitate towards.

But, when you decide to enable it, you get cool shit, which we - now that this is all out of the way - will now get into.

##### History

But first: History.

How do you even build camera streaming for a local-only vacuum robot webinterface on a custom proprietary vendor firmware?

##### Getting Video

Well, it starts with one thing. I don't know why the allwinner non-mainline kernel kept crashing.
It did however matter how hard we tried.

"We" being [@&#8203;Vivia](https://github.com/Vivia) here, who decided to take a break from hacking on gstreamer at work to do some recreational gstreamer hacking vacation instead.
The solution is `dustsrc`, which is a source element that does v4l, but just enough v4l to not scare the kernel.

<img src="https://github.com/user-attachments/assets/f1cb4291-1a1e-4518-a343-8a18a07512f8" />

You can find her talk about it here: <https://indico.freedesktop.org/event/11/contributions/516/>

As you can see, that was 2025, so a lot of patience with me coming to terms with that ethics dilemma was involved.
Thanks, Vivia!

##### Transmitting Video

So that's how we got frames, but how did we get them into the UI, given that Valetudo is http only, and with that cannot do webRTC.
The answer is yet again some FOSS wizardry.

13 years ago, [@&#8203;phoboslab](https://github.com/phoboslab) (which you probably know from other cool stuff like the [quite okay image format](https://qoiformat.org/)) likely must've noticed that the mpeg1 patents had expired, meaning that one could just use the codec for whatever one likes. The "whatever one likes" then became a [canvas/webGL based mpeg1 player in pure JS](https://jsmpeg.com/), which bypasses all of those limitations of "more modern" (and ~~Google~~ W3C-approved) ways of doing Video.

Now, 13 years later, it - in a vibe-mutilated form (sorry) - powers vacuum robot camera livestreaming.

And, since it's just http and no websockets, it should also just work with whatever reverse proxy setup you might be using.
Though, I have not actually tested that, so you tell me.

##### Rendering Video

Because normal video players with their "buffering" loading spinners and HUDs and all are boring, the way video is shown in Valetudo is stylized, imitating a 90s CRT displaying an analog RF Signal.

The way this was implemented was - of course - LLMs, so whether it actually has more soul than a dreadful spinny circle is debatable, but the result nevertheless fills me with joy.

<https://github.com/user-attachments/assets/45858dc4-f2e4-4c37-be88-8bbcda54cd2c>

As you can see, it clearly intuitively conveys the connection and stream state, without actually telling you the connection and stream state.
And it does so while also being fun.

I went for vibes but not necessarily full period correctness or accuracy, so if you're a shader wizard and better nostalgic hacker than me, feel free to open a PR to make it even more 90s CRT.
I'm after all just a fake suit pretending to be a hacker by glueing together the stuff of other people into a product.

##### Cool, how do I use it?

So that's the renderer thingy. But where can you see it?

First of all, on a dedicated camera page. It's boring in the sense that it just shows the camera and you can click on it for fullscreen.
You've already seen that one, so it's just shown briefly in the upcoming demo.

<https://github.com/user-attachments/assets/99037a58-4249-4dec-8778-2e2c7f9e66e3>

The next place is the manual controls view, which with that gains a live feed that shows you - with some lag for extra fun - where you're driving.

That works so well, I can't wait to see what people build/do with it.

<https://github.com/user-attachments/assets/e78b6abd-0e54-43ee-8b39-d8224255d08c>

Lastly, there's the spectator map, which combines video feed with map feed.
On desktop, you get a PiP mode, where you can cycle through PiP window sizes like you'd expect from the usual game minimap UX.
On mobile, you get a split view instead.

Valetudo-wise, that I think concludes everything that could sensibly be done with a video feed - with implementations I am very happy with, doing everything I would want them to do as a user.

##### The bigger picture

Here's the best part though:
This could extend beyond Valetudo.

Because this is literally just local http with no cloud dependency, account requirement or app-only limitations, you can build stuff on top of this.

We had a bunch of ideas already while brainstorming.

For example, what about VRchat (VR meaning vacuum robot), where you have a warehouse with robots with bidirectional audio (remember: they have microphones), allowing people to connect and interact with one another through the bot avatars.
Think metaverse but without the lizard.

All you need is like a queue proxy server in the cloud + some warehouse with bots. Less than 10k€ total for sure for a somewhat time-limited Art exhibit.

Or what about bot soccer using manual control?
Or the vacuum robot racing league?

Or maybe something like botroulette, where you can give a stranger 5 min of time to drive around your home + some contact form afterwards.

Or - like shown earlier to demonstrate manual control - an impromptu rover thingy.
Just pair the bot to your phone hotspot, then use the Valetudo UI on your phone to steer the robot somewhere outside without other wifi infrastructure.

There's so much stuff people could do just because this is not chained to a specific vendor-defined experience.
Remember when computers were that? General computation devices instead of prebuilt experiences to consume?

Anyway. I would love to see people do cool shit with this.
Have fun!

##### Some additional unordered thoughts

These did not fit into the narrative arc above, so here they are instead.

##### Mower cameras

I was at the local hardware store the other day and saw these mowers.
Can you see why I found them interesting?

<img src="https://github.com/user-attachments/assets/5e01acc9-a409-4d29-a55f-266ffbbea3ea" />

Their cameras point down to the ground, which is different from the vacuum robots which point straight forward - and with that see a lot more stuff not meant for their digital eyes.

##### IR cameras

Duststreamer also runs on the Roborock S8, which has this IR only cam.
That was interesting, because it saw nothing (besides its own IR lasers) while indoors with LED lights only.

However, take it outside and it starts seeing things

<img src="https://github.com/user-attachments/assets/2fb99e6c-cf5a-4a73-9b7e-d92da583c910" />

And, if you take it back inside, all you need is an old incandescent bulb and its waste IR radiation to see:

<img src="https://github.com/user-attachments/assets/7b5fe847-8f70-4287-b9e5-603b584ab420" />

##### Robot dashcam

Idk I just found this fun. I hope you'll find it fun too

<https://github.com/user-attachments/assets/29619285-e8ef-4f95-93c9-47eeb7f3175e>

#### The usual

If you like this release or Valetudo in general, you might want to consider donating:

<https://github.com/sponsors/Hypfer>

<https://builder.dontvacuum.me/donations.txt>

#### Autogenerated changelog

##### Features

- **vendor.midea**: Duststreaming [`7ddc02e`](http://github.com/Hypfer/Valetudo/commit/7ddc02e596edc3be66b4d53f4b4eaebac631f993)
- **vendor.roborock**: Duststreaming [`48545f2`](http://github.com/Hypfer/Valetudo/commit/48545f22081bac0d8b00df6d717af5de95cda2d2)
- **vendor.dreame**: Duststreaming [`90dc810`](http://github.com/Hypfer/Valetudo/commit/90dc810cd548db2e9cd45a620c2f0e1f791539d6)
- **ui**: Duststreaming [`d9fe988`](http://github.com/Hypfer/Valetudo/commit/d9fe988b32046b03638bf9041ba6ff85c9fd6ebe)
- **core**: Duststreaming [`fb39d93`](http://github.com/Hypfer/Valetudo/commit/fb39d9396bb082c9b140cf9859d9c6cb4793d0c0)

##### Fixes

- **ui**: Harmonize secondary labels [`5edb195`](http://github.com/Hypfer/Valetudo/commit/5edb19582047ab4d3e58d71e66cf91c2f4d30adb)
- **vendor.dreame**: The set of states that can be resumable extends beyond just docked [`7a2e09b`](http://github.com/Hypfer/Valetudo/commit/7a2e09b86b553710d5bda239de24e4eac768592e)
- **vendor.dreame**: Misc manual control improvements [`064cb88`](http://github.com/Hypfer/Valetudo/commit/064cb880eefc181610013e88eab71e68978cc662)
- **vendor.midea**: Fix semantic\_slam\_node crashlooping on J15 Max FW 558 [`ccda25e`](http://github.com/Hypfer/Valetudo/commit/ccda25ee46a64915116111da801ac7009f6e42b6)

##### Chores

- **release**: 2026.08.0 [`c249e72`](http://github.com/Hypfer/Valetudo/commit/c249e72abda4b8aa93c776562716085d58deb6dd)

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
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0My4yODguMCIsInVwZGF0ZWRJblZlciI6IjQzLjI4OC4wIiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJtaW5vci11cGRhdGUiXX0=-->
