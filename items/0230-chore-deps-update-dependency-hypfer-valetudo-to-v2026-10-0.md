# #230 chore(deps): update dependency hypfer/valetudo to v2026.10.0

- Kind: pull, state: closed, by renovate, created 2026-10-18T11:03:03-07:00
- Labels: none; milestone: none
- refs/pull/230/head -> main, merged: True

This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| [Hypfer/Valetudo](https://github.com/Hypfer/Valetudo) | minor | `2026.08.0` → `2026.10.0` |

`VALETUDO_SHA256` is refreshed automatically from the release assets; `test_constants.py` fails if a digest and its version comment ever disagree.

---

### Release Notes

<details>
<summary>Hypfer/Valetudo</summary>

### [`v2026.10.0`](https://github.com/Hypfer/Valetudo/releases/tag/2026.10.0): Valetudo 2026.10.0

[Compare Source](https://github.com/Hypfer/Valetudo/compare/2026.08.0...2026.10.0)

<div align="center">
    <img src="https://raw.githubusercontent.com/Hypfer/Valetudo/master/assets/logo/valetudo_logo_with_name.svg" width="800" alt="valetudo">
    <p align="center"><h2>2026.10.0</h2></p>
</div>

New robots, new Fab

##### Valetudo 2026.10.0

I have spent a lot of time working on Valetudo, without working directly on Valetudo.
While wrapping up, I saw that the first commit for the new bots was in *march*.

So, what changed?

1. We now have two modern MOVA robots that can run Valetudo in the same "it just works" way as always.
2. We also now have the Dustfab, which is the spiritual successor of the Dustbuilder, where I rewrote the thing (but not in Rust) to clean up 8 years of technical debt.
3. Lastly, we now have a new and improved Fastboot rooting flow (for the second time this year), using a helper binary that just does the right thing, packaged up in a way that is even easier to use.

... and on top, the Midea implementation should be more stable now.

##### The Dustfab

As you may have heard in the news, we have a severe global fab shortage.
Two releases ago, I've said that the new bots were blocked by infra.

So, I had to unblock that first, and to do that, eight years of technical debt, caused by a research prototype being put into production, had to be dealt with. This was (you might've already guessed it) "accelerated" by AI, in the sense that I told it to do 5 things, of which it did three, one of them with three layers of neurotic caches just in case.

So it still took quite some screaming and a while. But now it is done.

<img src="https://github.com/user-attachments/assets/3da2b90e-1cf0-441d-acdb-6a9e15ea73e3" />

The new thing

- Doesn't use E-Mail anymore
- Reacts faster
- Microservices (are they still cool?)
- Gives more feedback
- Is easier to maintain
- Docker (state-of-the-art (in 2015))
- Has a modern (wtf does that even mean) design
- Is dishwasher-safe
- Has cool captchas
- Cleans up previously confusing historically-grown wording
- Also is the new and improved checker

and, most importantly, comes with even more of my opinions.

I think you're gonna like it. I do like it very much.

\=> [fab.valetudo.cloud](https://fab.valetudo.cloud)

Moving this architecture under my roof also means that people should please stop pestering Dennis with nonsense.
He cannot help you. There is no point in wasting the time of that very busy and highly capable man with seven consecutive emails about.. I honestly don't even know what.

Seriously though, it makes zero sense to have someone on a level like him be bombarded with consumer spam, so now people can only bombard me, which I will nullroute.

##### The robots

It's MOVA. Surprise.
I had high hopes for Midea, but.. I mean they're alright-ish, but coming from a market-leading brand like Dreame, one did wonder why one would pay more for less.

So now we have some MOVAs, which is the more budget-brand spin-off of Dreame, where they mostly recycle previous-gen components of Dreame into new bots. But also sometimes not?
It is all very confusing.

These robots are based on a new platform, however, with the new and improved flow, that detail (and the friction and risks of it) should be pretty much invisible.
It was however very much not invisible while building up to this point. You're welcome.

I'd like to place a huge thank you to an anonymous collaborator here, who just showed up out of nowhere and had fun rooting new robots. Simply for the fun of "I'm gonna conquer this thing". Your work/thoughts helped (and continues to help) a lot, but, more importantly, just seeing someone hacking for the fun of hacking did *so much* for my morale.

##### Mova V50

I have to admit I really like this robot. The first time I saw it at someone else's place, I just thought "wow, nice".
Primarily, I think that is due to the new wheels (which I think premiered on the X50? Not sure), that are - effectively - silent.

Picture having seen probably over a hundred different vaccum robot models over the years with them all making similar-ish noises, and that guy just suddenly doesn't.

<img src="https://github.com/user-attachments/assets/d5dc307a-eb93-498b-ac6a-bc1eb3477eff" />

So that was cool. The rest of it is more of what we had on older dreames, but improved.
It can limit the max charge. It can now leap over higher thresholds. It can fit under lower furniture.

Haptics are good. The dock has a shape that is much better suited for a lot of spaces.

It's a good bot. I like it, and have been using it since march.

##### Mova Z60

I have to admit I don't really like this bot.

I bought it both out of curiosity and out of thinking that you people would likely be very happy with a roller mop with Valetudo support.
I kinda regret doing so. But by the time regret set in, I had already voided its warranty, so here we are.

<img src="https://github.com/user-attachments/assets/f25ba19c-f188-4b5c-8feb-7bb210b18a79" />

It is the V50 but worse. It is thicker, louder, more annoying and has a glossy dock.
The mop not only needs longer drying times but also constantly cycles its motor on and off during cleaning, which is an infuriating noise that annoys me two floors away.

The benefit I guess is supposed to be somewhat better mopping, but, honestly, I don't see it? At least not with my needs.
Maybe people with pets will see some value in it.

Either way, compromises were made, and I do not think that they were worth it.
But now there's a Valetudo-supported modern roller-mop-equipped bot.

> \[!NOTE]
> **Editor-Hypfer:**
> While wrapping up the release, I wanted to update the Amazon reflinks in the docs.
> Amazon did *not* let me generate a reflink for this one, as, apparently, too many people have returned it to them. So that's a datapoint.
>
> Of course, in true big-tech fashion, their wording is that I should *consider* linking something else. And then they hide the button. So you simply *can't* link it no matter if you consider or not.
> Corporate speak truly is a crime against humanity. Words have meanings. AGI 😭

##### What's next in robots?

Truth be told, I kinda hate putting this here, but I also know that people's brains have been fried, so that an announcement of two new things will always be met with "okay, what about thing 3 to n"? They don't say n of course, as that is weird nerd lingo.

Anyway, I do not know.

While I'd (as always) like to look into more robots, most of the new ones still.. kinda suck?
I'm just looking at them, thinking "no, I'm not paying money for that and certainly not the price you expect me to pay for it".

And you should feel the same, honestly.

I am observing the market and how prices and availability develops, but the X40 was fine, and, now that new availability of it has gotten a bit limited, the V50 is fine.
What else could anyone possibly want. And why.

While building Dustfab, for testing, I've picked up my Roborock S5 Max (2019) again, and gave it another spin.
Turns out that it's a perfectly fine vacuum robot, that gives you like 70% of what one of those 1500€ models would give you, but for.. idk like 30€ used?

Of course, buying used is not for everyone (me often included, tbh), but.. seriously just look at the market.
Oh and look at the recession we're in.

##### The fixes

As I am an idiot, I gave AI a spin to figure out why on Midea, Valetudo's phoenix failsafe sometimes has to kick in due to too much memory usage.

So it spent a few hours doing.. whatever, with me just watching. Eventually culminating in the AI rambling on about creating context and something about an S3 plan... which must've been a hallucination, given that we're running locally.

Anyway, Valetudo now reuses created TLS contexts, which may or may not fix that. It's kinda hard to reproduce unfortunately.

Additionally, the MQTT interface has been cleaned up and rid of unnecessary optionality.

And, lastly, the forced garbage collection logic has received some new thought after many years.
This should in many situations lead to lower CPU usage and better performance.

##### Closing thoughts

This.. took a while. As said further up, work started at the beginning of this year.

As you could perhaps slightly see, it was a fractal of work, where every work item was actually more work items.
On top, the daily grind happens, life happens, real work happens, real world happens.

One important thing to put here is that the new robots are somewhat less forgiving, in the sense that you have to pull the battery if you mess up - an action that requires destruction of warranty seals.

I did spend a lot of time improving the tooling there to prevent that, but the risk is still greater than on older bots. So there's that.

If you're curious what exactly "new tooling" means, check out the rewritten rooting instructions in the docs.

##### The usual

If you like this release or Valetudo in general, and/or you want me to look at more bots, you might want to consider donating:

<https://github.com/sponsors/Hypfer>

<https://builder.dontvacuum.me/donations.txt>

##### Autogenerated changelog

##### Features

- **vendor.dreame**: Map roller mop fluffer consumable and ignore the rest for now [`2e832db`](http://github.com/Hypfer/Valetudo/commit/2e832dbddca4f65ee4d1c1e5a99840019f832ea1)
- **vendor.dreame**: Mova V50/Z60 [`c0987f0`](http://github.com/Hypfer/Valetudo/commit/c0987f0b6c87d3f542db2d1ce3a0c6c6913ccc2d)
- **vendor.dreame**: Map two more error codes [`359a212`](http://github.com/Hypfer/Valetudo/commit/359a212e6897a31cea8df623b107fcb4a1373386)
- **ui**: Analytics [`49a73b0`](http://github.com/Hypfer/Valetudo/commit/49a73b0990d1227752fc501bbd5713af4f2a11bc)
- Some logo variants [`e048f8f`](http://github.com/Hypfer/Valetudo/commit/e048f8f10879b093de0bbeccfb0cb0d0624c43a7)
- **ui**: Add another spectator map pip window size [`4ca5609`](http://github.com/Hypfer/Valetudo/commit/4ca5609490b9cd85cfa2b25d4a808ccca109658a)

##### Fixes

- **mqtt**: Remove legacy cruft homie and home assistant optionality [`31bbc50`](http://github.com/Hypfer/Valetudo/commit/31bbc50c8dc34d02d4ed1a24d2febe0444344c64)
- **core**: Improve forced gc condition [`40e767d`](http://github.com/Hypfer/Valetudo/commit/40e767deb3607ece9b36e0af1643e59fdd1f270d)
- **mqtt**: Remove legacy cruft provideMapData option [`5772c9c`](http://github.com/Hypfer/Valetudo/commit/5772c9c39e6bdb2519c2217c0c4a77cf1ad95aaa)
- **vendor.midea**: Reuse secureContext like a sane person that read the docs [`b41e048`](http://github.com/Hypfer/Valetudo/commit/b41e048de73e35f39f7fbfb604f1555bb8d45cd4)
- **vendor.dreame**: Try a different ID [`b69ab0b`](http://github.com/Hypfer/Valetudo/commit/b69ab0b2c8637c1cf32dc52eac0c0ea3a3d4f745)
- **vendor.midea**: Handle unhandled water_level [`c01fb4b`](http://github.com/Hypfer/Valetudo/commit/c01fb4b0d3431e8d51067806d21ff9a289e4156b)
- **ui**: Make my head hurt less [`1ead8e8`](http://github.com/Hypfer/Valetudo/commit/1ead8e8bea0af8f8bfd2a1d280c236f6f5b75947)
- **core**: Give the duststream clients some grace in handling packets [`fc43380`](http://github.com/Hypfer/Valetudo/commit/fc433805db959a878c9861f66997ec1ab3974894)
- **vendor.midea**: Handle some unknown pixel types [`fa2c999`](http://github.com/Hypfer/Valetudo/commit/fa2c9997cfaabc097d8a3a5a337927d1ba8e0a47)
- Use stat as intended in isDuststreamerInstalled [`1783c55`](http://github.com/Hypfer/Valetudo/commit/1783c55412e510518f68bd75e80faac25e3e7c41)

##### Refactoring

- **duststreaming**: Punch some more intent into the code [`b6c0896`](http://github.com/Hypfer/Valetudo/commit/b6c0896208d72435701aba130ee9e5abf24474bc)

##### Chores

- **release**: 2026.10.0 [`7826751`](http://github.com/Hypfer/Valetudo/commit/78267517d3036258322b6760ba8a9979d197e249)
- Formatting [`190816d`](http://github.com/Hypfer/Valetudo/commit/190816dbe8c45c5f7256ef87ca1ffb04ecb6da78)
- Precision [`4651c19`](http://github.com/Hypfer/Valetudo/commit/4651c1979ba8528137948e30b94da9e7441148f6)
- Put the LLMs in their place [`328dd39`](http://github.com/Hypfer/Valetudo/commit/328dd39c17d6d3c3010b749b6bf9d56da7c30be4)

</details>

---

### Configuration

📅 **Schedule**: (UTC)

- Branch creation
  - At any time (no schedule defined)
- Automerge
  - At any time (no schedule defined)

🚦 **Automerge**: Enabled.

♻ **Rebasing**: Whenever PR is behind base branch, or you tick the rebase/retry checkbox.

🔕 **Ignore**: Close this PR and you won't be reminded about this update again.

---

 - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box

---

This PR has been generated by [Mend Renovate CLI](https://github.com/renovatebot/renovate).
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0NC4xNTEuMiIsInVwZGF0ZWRJblZlciI6IjQ0LjE1MS4yIiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJtaW5vci11cGRhdGUiXX0=-->
