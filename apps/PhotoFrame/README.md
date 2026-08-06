# PhotoFrame

A personalized digital photo frame app that displays rotating photos of Sarah with captions and music, accessible via a tablet in the living room.

## Features

- Rotating photo display with configurable timing
- Custom captions for each photo
- Background music playback
- Touch controls for navigation
- Settings for timing, music, and display options

## Build

```bash
cd apps/PhotoFrame
xcodebuild -scheme PhotoFrame -configuration Release \
  -derivedDataPath /tmp/PhotoFrame-build build
open /tmp/PhotoFrame-build/Build/Products/Release/PhotoFrame.app
```

Or open `PhotoFrame.xcodeproj` in Xcode and run.
