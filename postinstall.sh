#!/bin/bash

COMMAND=$0
PTEMPDIR=$1
PSHNAME=$2
PDIR=$3
PVERSION=$4

echo "<INFO> Command is: $COMMAND"
echo "<INFO> Plugin name is: $PSHNAME"
echo "<INFO> Plugin folder is: $PDIR"
echo "<INFO> Plugin version is: $PVERSION"

PCONFIG=$LBHOMEDIR/config/plugins/$PDIR

if [ ! -f "$PCONFIG/smartmii.json" ]; then
    echo "<INFO> Creating default configuration..."
    cp $PTEMPDIR/config/smartmii.json $PCONFIG/smartmii.json
else
    echo "<INFO> Configuration already exists, keeping it"
fi

echo "<OK> Post-install completed"
exit 0
