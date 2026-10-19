#!/usr/bin/perl
# A stand-in for `dotnet VintagestoryServer.dll`, for the image test (no game, no network). It
# behaves like Vintage Story 1.22.7 where the supervisor and flux-vs depend on it (all measured):
#
#   - `--genconfig` writes a default serverconfig.json and exits
#   - its log is <logPath>/server-main.log, lines "dd.mm.yyyy HH:MM:SS [Notification] ..."
#   - "Dedicated Server now running on Port <port> and all ips!" once it is up
#   - the console is stdin: "Handling Console Command <cmd>", then the answer
#   - SIGTERM: saves ("World saved!") and exits 0
#
# It renames its own command line to "dotnet VintagestoryServer.dll --dataPath ..." ($0), which
# is the pattern the supervisor finds the server by.
use strict;
use warnings;
use IO::Select;
use POSIX qw(strftime);

my %opt;
my @args = @ARGV;
shift @args if @args && $args[0] =~ /VintagestoryServer\.dll$/;
while (@args) {
  my $a = shift @args;
  if ($a eq '--genconfig') { $opt{genconfig} = 1; next; }
  if ($a =~ /^--(dataPath|logPath|archiveLogFileCount)$/) { $opt{$1} = shift @args; next; }
}
my $data = $opt{dataPath} or die "no --dataPath\n";
my $logdir = $opt{logPath} || "$data/Logs";

if ($opt{genconfig}) {
  open(my $c, '>', "$data/serverconfig.json") or die;
  print $c <<"JSON";
{
  "ConfigVersion": "1.10", "ServerName": "Vintage Story Server", "Port": 42420, "MaxClients": 16,
  "AdvertiseServer": false, "Password": null, "WhitelistMode": 0, "ModPaths": ["Mods", "$data/Mods"],
  "Roles": [{"Code": "suplayer", "Name": "Survival Player", "PrivilegeLevel": 0}, {"Code": "admin", "Name": "Admin", "PrivilegeLevel": 99999}],
  "DefaultRoleCode": "suplayer", "MapSizeX": 1024000, "MapSizeZ": 1024000,
  "WorldConfig": {"Seed": null, "SaveFileLocation": "$data/Saves/default.vcdbs", "WorldName": "A new world",
                  "PlayStyle": "surviveandbuild", "WorldType": "standard", "WorldConfiguration": null}
}
JSON
  close $c;
  exit 0;
}

$0 = "dotnet VintagestoryServer.dll --dataPath $data --logPath $logdir";
mkdir $logdir;
open(my $log, '>>', "$logdir/server-main.log") or die "log: $!";
$log->autoflush(1);
STDOUT->autoflush(1);
sub say_log {
  for my $line (@_) {
    my $l = strftime('%d.%m.%Y %H:%M:%S', localtime) . " [Notification] $line\n";
    print $log $l;
    print STDOUT $l;
  }
}

my $cfg = do { local $/; open(my $f, '<', "$data/serverconfig.json") or die; <$f> };
my ($port) = $cfg =~ /"Port":\s*(\d+)/;
my ($save) = $cfg =~ /"SaveFileLocation":\s*"([^"]+)"/;
my ($maxc) = $cfg =~ /"MaxClients":\s*(\d+)/;
$port ||= 42420;
mkdir "$data/Saves";
unless (-e $save) { open(my $s, '>', $save) or die "save: $!"; print $s "SQLite stand-in\n" x 100; close $s; }
my $started = time;

my $stop = 0;
$SIG{TERM} = sub { $stop = 1; };
say_log('Server logger started.', 'Game Version: v1.22.7 (Stable)');
select(undef, undef, undef, 1.0);
say_log("Dedicated Server now running on Port $port and all ips!");

my %wc = (temporalStorms => 'sometimes', playerlives => '-1');
my $sel = IO::Select->new(\*STDIN);
while (!$stop) {
  next unless $sel->can_read(0.2);
  my $cmd = <STDIN>;
  last unless defined $cmd;
  chomp $cmd;
  next if $cmd eq '';
  say_log("Handling Console Command $cmd");
  if ($cmd eq '/stats') {
    my $up = time - $started;
    say_log("Version: 1.22.7\nUptime: 0 days, 0 hours, 0 minutes, $up seconds\nPlayers online: 1 / $maxc\n\nMemory usage Managed/Total: 600.0Mb / 1000 Mb\nLast 2s Ticks/s: 30\nLoaded chunks: 16\nNetwork TCP: 0 Packets/s or 0 Kb/s\nNetwork UDP: 0 Packets/s or 0 Kb/s");
  } elsif ($cmd eq '/list clients') {
    say_log("List of online Players\nPlaying [1] Tester 203.0.113.9:50000 (42ms)\n");
  } elsif ($cmd eq '/time') {
    say_log("Server time: 3. June, Year 1, 14:20\nGame Speed: 48 IRL minutes");
  } elsif ($cmd eq '/info seed') {
    say_log('424242');
  } elsif ($cmd =~ m{^/worldconfig (\w+)(?: (\S+))?$}) {
    if (defined $2) { $wc{$1} = $2; say_log("Ok, value $2 set. Restart game world or server to apply changes."); }
    elsif (exists $wc{$1}) { say_log("$1 currently has value: $wc{$1}"); }
    else { say_log("No such config found: $1"); }
  } elsif ($cmd =~ m{^/genbackup (\S+)$}) {
    mkdir "$data/Backups";
    system('cp', $save, "$data/Backups/$1");
    say_log('Backup created');
  } elsif ($cmd =~ m{^/op (\S+)$}) {
    say_log("Ok, role Admin assigned to $1");
  } else {
    say_log("Command not found");
  }
}
say_log('Server termination event SIGTERM received. Shutting down server', 'World saved! Saved 8 chunks, 1 mapchunks, 16 mapregions.', 'Stopped the server!');
exit 0;
