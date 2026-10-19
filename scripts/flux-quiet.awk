# The server's stdout, minus the answers to the console commands the panel polls with.
#
#   awk -v quiet='<regex of commands>' -f flux-quiet.awk
#
# The game logs "<date> <time> [Server Notification] Handling Console Command <cmd>", then its
# answer: one dated line, and for some commands undated lines after it (/stats, /time,
# /list clients: measured on 1.22.7). After a quiet command, its echo, the next dated line and the
# undated lines after that are dropped; everything else passes, unbuffered.
function dated(line) { return line ~ /^[0-9][0-9]\.[0-9][0-9]\.[0-9][0-9][0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9] / }
{
  if (index($0, "Handling Console Command ") > 0) {
    cmd = substr($0, index($0, "Handling Console Command ") + 25)
    sub(/[ \r]+$/, "", cmd)
    if (quiet != "" && cmd ~ quiet) { skip = 1; answered = 0; next }
    skip = 0
  } else if (skip) {
    if (dated($0)) {
      if (answered) { skip = 0 } else { answered = 1; next }
    } else {
      next
    }
  }
  print
  fflush()
}
