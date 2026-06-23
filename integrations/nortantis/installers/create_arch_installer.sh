#!/bin/bash
set -e

nortantis_version=$(cat version.txt)

# Build the jar
pushd ..
./gradlew :jar
popd

# Copy files makepkg expects as local sources
cp "../build/libs/Nortantis.jar" "nortantis.jar"
cp "taskbar icon.png" "nortantis.png"

# Stamp the version into the PKGBUILD
sed -i "s/^pkgver=.*/pkgver=$nortantis_version/" PKGBUILD

# Build the package
makepkg --nodeps --skipinteg

# Clean up temporary files
rm -f nortantis.jar nortantis.png
rm -rf src pkg
