---
layout: post
title: Rethinking NCAA Football CFP Rankings using PageRank Adaptations
published: true
---

The current system of determining College Football Playoff Rankings is potentially subject to a number of biases, and Google’s PageRank algorithm may offer a fix.

Note: This post was reworked from my original post [here](https://medium.com/@pdubslax/ncaa-football-rankings-using-pagerank-adaptions-f880f75b8c9b)

The current process involves a lot of complex metrics and some amount of arbitrary decision by a committee who looks at game film. This effectively makes the algorithm a black box to teams and spectators alike.

I wanted to create a simpler ranking system that only considered wins and losses. The hypothesis was that the current system may overfit based on a number of specific metrics that certain teams play styles would better fit with. For example, teams react differently in a blowout: second stringers may go in, game strategy may change, etc.

To combat all this, I was curious to see what would happen if you calculated the rankings solely based on the wins and losses of a team. In theory, a team could be treated as a single entity (instead of a collection of smaller and overlapping performant units), preventing individual players, or coaches decisions from spiking the teams overall rank value.

### My Original Idea: Total wins of teams who you beat (TWINS)

This ranking is simply a one hop calculation where your teams score is determined by the sum of team wins that every team who you beat has. For example:
The Michigan Wolverines (as of Nov 9th) have beaten: Appalachian State (4–5), Miami of Ohio (2–8), Penn State (5–4), Indiana (3–6), and Northwestern (3–6). This yields a TWINS score of 4+2+5+3+3 = 17. Good enough for a Number 56 overall rating.

The top 15 after running this calculation are as follows:

![TWINS Rankings](/images/page-rank-twins-rankings.png)

These ratings are interesting and pretty similar to the current ratings with major exceptions including UCLA ranked unusually high. I checked out their schedule and this is caused by wins over two great teams in ASU and Arizona.

For my Michigan State friends out there, the Spartans are ranked 32nd with this implementation. However, a victory against OSU last night would have put them at 12th overall.

Interested to see what you all think of this. Variations could include looking at multiple hops in addition to just the one. A drawbacks of this algorithm includes number of games played as a determining factor which affects mid season rankings but could be easily normalized if need be, by games played.

This line of thought lead me to an additional method derived from PageRank that I believe really highlights from interesting information.

### My Second Idea: PageRank with losses as edges in directed graph (PARK)

PageRank is an algorithm named after Larry Page of Google and is used to determine the importance of webpages ranked in relation to all the other sites in a set. It works by assigning a score to each webpage that is determined by the number of incoming links and the scores of those webpages which the incoming links come from.

Having taken a few classes that touch on the subject of information retrieval, classifying, and machine learning, I was HYPE to test out a variation of this algorithm tailored to the College Football Ranking problem.

My solution basically treats teams as nodes and each individual game played as an edge on the directed graph. Margin of victory is not taken into account intentionally due to some of the reasons mentioned above. The parameters include a damping factor that, in this context, represents the likelihood that a team who is supposed to beat a team, will lose. So tampering with this param will slightly alter the results.

In the context of football, every team has a score. And every team that a given team loses to gets an equal portion of that score. So say you beat 7 teams; the better those teams are, and the fewer times they have losses, the higher your score will be!

All in all, this algorithm essentially takes into consideration EVERY game played over the course of this entire season. Because of schools scheduling out of conference games, there are no isolated pools and every team’s score is effected by every other team in some way or another. The only manipulation was that I added a self loop edge for the undefeated teams so that those teams would receive the benefit of their own lack of losses.

The top 15 after running this calculation are as follows:

![PARK Rankings](/images/page-rank-park-rankings.png)

This was really what I was looking for. What were the teams who are extremely underrated/overrated with todays system?

UCLA… With wins over ASU and Arizona, my implementation states that this two loss team deserves a spot in the 4 team playoff. A truly interesting result. Interestingly, no love for Notre Dame especially after that loss last night.

The Big Ten also gets NO LOVE from this system and probably rightly so considering out out of conference play.

Michigan State is ranked: 20th (ahead of Ohio State)

Ohio State is ranked: 22nd

Michigan is ranked: 66th #goblue

I just cranked algo/post out this out this morning so it is by no means polished, but there is some really cool potential here in how we view existing ranking systems. Should all these intangibles be taken into consideration? Or should we just let wins speak for themselves?
