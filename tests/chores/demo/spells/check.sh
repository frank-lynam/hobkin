#!/bin/sh
# The watch spell: prints only when there's litter in $CHORE_FOLDER (silence means all's well).
n=$(find "$CHORE_FOLDER" -maxdepth 1 -name "$CHORE_PATTERN" | wc -l)
[ "$n" -gt 0 ] && echo "$n pieces of litter in $CHORE_FOLDER"
exit 0
