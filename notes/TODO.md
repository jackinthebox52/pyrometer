Debate and chat about how to normalize graphs when comparing data. Some graphs end up plotting straight 
lines becuase the scales are different between compared runs.

DONE - Make graphs resizeable or expandable in gui

DONE - Add option to toggle run data within each graph of a comparison,. allowingus to compare 2 out of our 
overall set 

Implement complex run ranking factoring in overfitting, eval accuracy, eval loss, grad norm, even power 
draw and time to rank for efficiency. Could render a cool leaderboard at each comparison.


Fix colors in kernel timelines. There are too many that are not easily differentiable. Multiple types of green, etc.

If you scroll all the way in or out over a kernel timeline then continue to scroll, the entire page scrolls up or down. That is clunky and annoying. Fix

Gpu stats simply show usage or temp etc. as measured during a training run, not as known to be USED BY a training run. If you run various training runs at the same time, all of them will show 100% gpu usage, becuase that's what they measured. Fix.

Fix anomaly detection. It is wayyy too sensitive.

Events should probably be seperated by graph when overlaying them. Some are not important for most graphs.

We should add to the sdk the ability to write arbitrary statistics to a run which are then dymaically rendered along with the standard loss and accuracy graphs. This would be very helpful for custom model architectures like HMN where we have additional metrics.

We need some type of experiment grouping and overall better organizational features.

Model visualizer. Select a checkpoint and open up a network map that shows all weights and biases and learned parameters when hovering over nodes.
