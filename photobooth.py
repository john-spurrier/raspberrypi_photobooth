import os
import time
import datetime
import subprocess
import pygame

# picamera2 lets us pull frames directly into a numpy array (and therefore a
# pygame surface) so we can draw the live camera feed as the background of
# the countdown screen. It only exists on a Raspberry Pi, so we import it
# lazily and fall back to the legacy rpicam-still subprocess path on dev
# machines or if the user hasn't installed it (`sudo apt install python3-picamera2`).
try:
    from picamera2 import Picamera2
    HAVE_PICAMERA2 = True
except Exception:                       # pragma: no cover - environment dependent
    HAVE_PICAMERA2 = False

__author__ = "digiiash"
__license__ = "MIT"
__version__ = "2.0.0"

# ── Capture configuration ──────────────────────────────────────────────────────
NUM_PHOTOS      = 3        # photos per session (classic photo-strip = 3)
COUNTDOWN_SECS  = 3        # seconds to count down before each shot
COUNTDOWN_HOLD  = 1.0      # seconds to display each countdown number
FLASH_SETTLE    = 0.05     # short delay so the white frame is on screen
                           # before we stop the preview / start capture
CAPTURE_TIMEOUT = 1200     # raspistill capture timeout in ms (lets AWB settle)
PREVIEW_SETTLE  = 0.4      # seconds to wait after starting preview
CAPTURE_WIDTH   = 1024     # captured photo resolution (kept modest for speed)
CAPTURE_HEIGHT  = 768

# Raspberry Pi OS Bookworm and newer ship libcamera; raspistill no longer
# exists. The capture/preview helpers below use the libcamera-still flag set.
CAPTURE_CMD = "rpicam-still"

# When picamera2 is available we render the camera feed straight into the
# pygame fullscreen surface during the countdown so the subject can see
# themselves pose. If picamera2 isn't installed we silently fall back to a
# plain black countdown background and capture via rpicam-still.
SHOW_PREVIEW    = True
MIRROR_PREVIEW  = True     # flip horizontally so it behaves like a selfie cam
PREVIEW_FPS     = 30       # cap preview redraws so we don't peg the CPU
CAMERA_WARMUP   = 0.6      # let AWB/AGC settle after starting the camera

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR  = os.path.join(SCRIPT_DIR, "images")
PHOTOS_DIR  = os.path.join(SCRIPT_DIR, "photos")
TMP_DIR     = os.path.join(SCRIPT_DIR, "tmp")
ATTRACT_PATH = os.path.join(IMAGES_DIR, "attract.jpg")

# ── Strip layout ───────────────────────────────────────────────────────────────
STRIP_W      = 600                              # logical strip width  (px)
STRIP_PAD    = 24                               # outer white border
PHOTO_GAP    = 16                               # vertical gap between photos
FOOTER_H     = 90                               # bottom label area
STRIP_BG     = (255, 255, 255)
FOOTER_FG    = (40, 40, 40)
FOOTER_TEXT  = "PHOTOBOOTH"

PHOTO_W      = STRIP_W - STRIP_PAD * 2
PHOTO_H      = PHOTO_W * 3 // 4                 # 4:3 cells
STRIP_H      = (STRIP_PAD * 2
                + PHOTO_H * NUM_PHOTOS
                + PHOTO_GAP * (NUM_PHOTOS - 1)
                + FOOTER_H)

# ── Countdown overlay style ────────────────────────────────────────────────────
FONT_SIZE      = 280
NUMBER_COLOR   = (255, 255, 255)
NUMBER_ALPHA   = 230
SHADOW_COLOR   = (0, 0, 0)
SHADOW_ALPHA   = 150
SHADOW_OFFSET  = 8
CIRCLE_COLOR   = (0, 0, 0)
CIRCLE_ALPHA   = 120
CIRCLE_RADIUS  = 170

# ── On-screen buttons ──────────────────────────────────────────────────────────
BTN_FONT_SIZE = 64
BTN_PAD_X     = 60
BTN_PAD_Y     = 28
BTN_RADIUS    = 18
BTN_FG        = (255, 255, 255)
BTN_BG_START  = (220, 50, 90)
BTN_BG_SAVE   = (40, 170, 90)
BTN_BG_REDO   = (60, 110, 200)

# ── Close (×) button ──────────────────────────────────────────────────────────
CLOSE_BG       = (200, 30, 30)   # red circle
CLOSE_FG       = (255, 255, 255) # white X strokes
CLOSE_SIZE     = 90              # button diameter in px (touch-friendly)
CLOSE_MARGIN   = 24              # gap from the top/right edge
CLOSE_STROKE   = 8               # thickness of the X strokes
# ──────────────────────────────────────────────────────────────────────────────


# ── Display helpers ────────────────────────────────────────────────────────────
def load_attract(screen_size):
    """Load the attract image, generating a dark placeholder if missing."""
    if os.path.exists(ATTRACT_PATH):
        img = pygame.image.load(ATTRACT_PATH).convert()
        return pygame.transform.scale(img, screen_size)

    print(f"[WARNING] Missing image: {ATTRACT_PATH}")
    surf = pygame.Surface(screen_size)
    surf.fill((20, 20, 20))
    font = pygame.font.SysFont(None, 96, bold=True)
    label = font.render("PHOTOBOOTH", True, (220, 220, 220))
    surf.blit(label, label.get_rect(center=(screen_size[0] // 2,
                                             screen_size[1] // 3)))
    return surf


def draw_button(screen, text, font, center, bg):
    """Draw a rounded-rect button with a text label. Returns its rect."""
    label = font.render(text, True, BTN_FG)
    w = label.get_width()  + BTN_PAD_X * 2
    h = label.get_height() + BTN_PAD_Y * 2
    rect = pygame.Rect(0, 0, w, h)
    rect.center = center
    pygame.draw.rect(screen, bg, rect, border_radius=BTN_RADIUS)
    screen.blit(label, label.get_rect(center=rect.center))
    return rect


def draw_close_button(screen, screen_size):
    """
    Draw a red circular close (×) button in the top-right corner and return
    its bounding rect. Lines are drawn rather than rendered as text so the
    glyph stays crisp regardless of font availability.
    """
    sw, _ = screen_size
    rect = pygame.Rect(0, 0, CLOSE_SIZE, CLOSE_SIZE)
    rect.topright = (sw - CLOSE_MARGIN, CLOSE_MARGIN)
    cx, cy = rect.center
    radius = CLOSE_SIZE // 2

    pygame.draw.circle(screen, CLOSE_BG, (cx, cy), radius)

    # X strokes, inset from the circle edge so they don't touch the rim.
    inset = CLOSE_SIZE // 4
    pygame.draw.line(
        screen, CLOSE_FG,
        (rect.left + inset,  rect.top + inset),
        (rect.right - inset, rect.bottom - inset),
        CLOSE_STROKE,
    )
    pygame.draw.line(
        screen, CLOSE_FG,
        (rect.right - inset, rect.top + inset),
        (rect.left + inset,  rect.bottom - inset),
        CLOSE_STROKE,
    )
    return rect


def wait_for_click(rects):
    """
    Block until the user taps/clicks within one of the given rects.
    `rects` may be a single Rect or a list of Rects.
    Returns the 0-based index of the rect that was hit, or -1 to quit.
    """
    if isinstance(rects, pygame.Rect):
        rects = [rects]

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return -1
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return -1
            if event.type == pygame.MOUSEBUTTONDOWN:
                for i, r in enumerate(rects):
                    if r.collidepoint(event.pos):
                        return i
            # Treat touchscreen FINGERDOWN events as well (SDL2)
            if hasattr(pygame, "FINGERDOWN") and event.type == pygame.FINGERDOWN:
                info = pygame.display.Info()
                pos = (int(event.x * info.current_w),
                       int(event.y * info.current_h))
                for i, r in enumerate(rects):
                    if r.collidepoint(pos):
                        return i
        pygame.time.wait(20)


# ── Camera helpers ─────────────────────────────────────────────────────────────
def init_camera():
    """
    Start picamera2 in a video configuration sized for capture. Returns the
    running Picamera2 instance, or None if picamera2 isn't installed / fails
    to start (in which case the rest of the script falls back to the
    rpicam-still subprocess path with no live preview).
    """
    if not (SHOW_PREVIEW and HAVE_PICAMERA2):
        return None
    try:
        picam2 = Picamera2()
        # NOTE on the "BGR888" string: picamera2 uses the V4L2 naming
        # convention where the format name describes channel order from MSB
        # to LSB of each pixel, which is the opposite of how the bytes sit
        # in memory. "BGR888" actually lays bytes out as R, G, B per pixel
        # -- exactly what pygame.image.frombuffer(..., "RGB") expects.
        config = picam2.create_video_configuration(
            main={"size": (CAPTURE_WIDTH, CAPTURE_HEIGHT), "format": "BGR888"},
        )
        picam2.configure(config)
        picam2.start()
        time.sleep(CAMERA_WARMUP)
        return picam2
    except Exception as exc:
        print(f"[WARNING] Could not start picamera2 preview: {exc}")
        return None


def stop_camera(picam2):
    """Shut down the picamera2 instance cleanly."""
    if picam2 is None:
        return
    try:
        picam2.stop()
        picam2.close()
    except Exception as exc:
        print(f"[WARNING] Error stopping picamera2: {exc}")


def grab_preview_surface(picam2, screen_size):
    """
    Capture one frame from picamera2 and return it as a pygame surface that
    has been cover-scaled (and optionally mirrored) to fill the screen.
    Returns None if no frame is available.
    """
    if picam2 is None:
        return None
    try:
        arr = picam2.capture_array("main")            # shape (H, W, 3), RGB bytes
    except Exception as exc:
        print(f"[WARNING] Preview frame error: {exc}")
        return None

    h, w = arr.shape[:2]
    frame = pygame.image.frombuffer(arr.tobytes(), (w, h), "RGB")
    if MIRROR_PREVIEW:
        frame = pygame.transform.flip(frame, True, False)

    sw, sh = screen_size
    scale = max(sw / w, sh / h)                       # cover-scale, no letterbox
    new_w, new_h = int(w * scale), int(h * scale)
    if (new_w, new_h) != (w, h):
        frame = pygame.transform.scale(frame, (new_w, new_h))
    return frame, ((sw - new_w) // 2, (sh - new_h) // 2)


def capture_photo(picam2, path):
    """
    Snap a single JPEG to `path`. The screen should already be white when
    this is called so the subject is lit during the exposure.
    Uses picamera2 when available, otherwise shells out to rpicam-still.
    """
    if picam2 is not None:
        try:
            picam2.capture_file(path)
            return
        except Exception as exc:
            print(f"[WARNING] picamera2 capture failed, falling back: {exc}")

    subprocess.call([
        CAPTURE_CMD,
        "-t", str(CAPTURE_TIMEOUT),
        "--width", str(CAPTURE_WIDTH),
        "--height", str(CAPTURE_HEIGHT),
        "-n",
        "-o", path,
    ])


# ── Countdown + flash ──────────────────────────────────────────────────────────
def draw_countdown_frame(screen, number, screen_size, font_large, picam2):
    """
    Draw one frame of the countdown:
      - live camera preview as the background (or black if no camera)
      - a semi-transparent dark backing circle
      - a drop shadow + the countdown number on top
    """
    w, h = screen_size
    cx, cy = w // 2, h // 2

    bg = grab_preview_surface(picam2, screen_size)
    if bg is None:
        screen.fill((0, 0, 0))
    else:
        frame, pos = bg
        screen.blit(frame, pos)

    circle_surf = pygame.Surface(
        (CIRCLE_RADIUS * 2, CIRCLE_RADIUS * 2), pygame.SRCALPHA
    )
    pygame.draw.circle(
        circle_surf,
        (*CIRCLE_COLOR, CIRCLE_ALPHA),
        (CIRCLE_RADIUS, CIRCLE_RADIUS),
        CIRCLE_RADIUS,
    )
    screen.blit(circle_surf, (cx - CIRCLE_RADIUS, cy - CIRCLE_RADIUS))

    shadow_surf = font_large.render(str(number), True, SHADOW_COLOR)
    shadow_surf.set_alpha(SHADOW_ALPHA)
    screen.blit(
        shadow_surf,
        shadow_surf.get_rect(center=(cx + SHADOW_OFFSET, cy + SHADOW_OFFSET)),
    )

    num_surf = font_large.render(str(number), True, NUMBER_COLOR)
    num_surf.set_alpha(NUMBER_ALPHA)
    screen.blit(num_surf, num_surf.get_rect(center=(cx, cy)))

    pygame.display.flip()


def run_countdown(screen, screen_size, font_large, picam2, clock):
    """
    Count down from COUNTDOWN_SECS to 1, holding each digit on screen for
    COUNTDOWN_HOLD seconds while continuously refreshing the live preview
    behind it (so the image doesn't freeze between numbers).
    """
    for n in range(COUNTDOWN_SECS, 0, -1):
        end_time = time.monotonic() + COUNTDOWN_HOLD
        while time.monotonic() < end_time:
            draw_countdown_frame(screen, n, screen_size, font_large, picam2)
            pygame.event.pump()
            clock.tick(PREVIEW_FPS)


def flash_white(screen):
    """Fill the entire screen white — used as fill light during capture."""
    screen.fill((255, 255, 255))
    pygame.display.flip()


def shoot_photo(screen, screen_size, font_large, out_path, shot_num, total,
                picam2, clock):
    """One full shoot cycle: live-preview countdown + flash + capture."""
    run_countdown(screen, screen_size, font_large, picam2, clock)

    flash_white(screen)
    time.sleep(FLASH_SETTLE)
    capture_photo(picam2, out_path)

    # Keep flashing white briefly so the transition looks intentional, then
    # clear to black so the next countdown starts from a clean frame.
    flash_white(screen)
    time.sleep(0.1)
    screen.fill((0, 0, 0))
    pygame.display.flip()

    print(f"  Captured photo {shot_num}/{total} -> {out_path}")


# ── Strip composition + review ─────────────────────────────────────────────────
def compose_strip(photo_paths, font_footer):
    """Combine captured photos into a classic vertical photo-strip surface."""
    strip = pygame.Surface((STRIP_W, STRIP_H))
    strip.fill(STRIP_BG)

    y = STRIP_PAD
    for path in photo_paths:
        cell = pygame.Surface((PHOTO_W, PHOTO_H))
        cell.fill((40, 40, 40))

        if os.path.exists(path):
            try:
                img = pygame.image.load(path).convert()
                iw, ih = img.get_size()
                # Cover-scale: keep aspect ratio, fill the cell, crop center
                scale = max(PHOTO_W / iw, PHOTO_H / ih)
                sw, sh = int(iw * scale), int(ih * scale)
                scaled = pygame.transform.smoothscale(img, (sw, sh))
                crop_x = (sw - PHOTO_W) // 2
                crop_y = (sh - PHOTO_H) // 2
                cell.blit(scaled, (-crop_x, -crop_y))
            except pygame.error as exc:
                print(f"[WARNING] Could not load {path}: {exc}")

        strip.blit(cell, (STRIP_PAD, y))
        y += PHOTO_H + PHOTO_GAP

    # Footer label
    footer_surf = font_footer.render(FOOTER_TEXT, True, FOOTER_FG)
    footer_center_y = STRIP_H - FOOTER_H // 2
    strip.blit(
        footer_surf,
        footer_surf.get_rect(center=(STRIP_W // 2, footer_center_y)),
    )

    # Subtle date stamp under the footer text
    stamp = datetime.datetime.now().strftime("%b %d, %Y")
    date_font = pygame.font.SysFont(None, 28)
    date_surf = date_font.render(stamp, True, FOOTER_FG)
    strip.blit(
        date_surf,
        date_surf.get_rect(center=(STRIP_W // 2, footer_center_y + 30)),
    )

    return strip


def show_review(screen, screen_size, strip_surf, font_btn):
    """Show the strip with SAVE / REDO buttons; returns 'save', 'redo' or 'quit'."""
    sw, sh = screen_size

    screen.fill((25, 25, 25))

    # Reserve the bottom band for the buttons, then scale the strip
    # to fit the remaining area while preserving aspect ratio.
    btn_band_h = 160
    max_h = sh - btn_band_h - 40
    max_w = sw - 80
    scale = min(max_h / STRIP_H, max_w / STRIP_W)
    new_w = int(STRIP_W * scale)
    new_h = int(STRIP_H * scale)
    scaled = pygame.transform.smoothscale(strip_surf, (new_w, new_h))
    screen.blit(
        scaled,
        scaled.get_rect(center=(sw // 2, (sh - btn_band_h) // 2)),
    )

    btn_y = sh - btn_band_h // 2
    redo_rect = draw_button(screen, "REDO", font_btn,
                            (sw // 2 - 200, btn_y), BTN_BG_REDO)
    save_rect = draw_button(screen, "SAVE", font_btn,
                            (sw // 2 + 200, btn_y), BTN_BG_SAVE)
    close_rect = draw_close_button(screen, screen_size)
    pygame.display.flip()

    idx = wait_for_click([save_rect, redo_rect, close_rect])
    if idx == 0:
        return "save"
    if idx == 1:
        return "redo"
    return "quit"   # idx == 2 (× pressed) or -1 (ESC / window close)


def save_strip(strip_surf):
    """Save the composed strip to disk with a timestamped filename."""
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PHOTOS_DIR, f"strip_{stamp}.jpg")
    pygame.image.save(strip_surf, path)
    print(f"Saved strip: {path}")
    return path


# ── Attract screen + session runner ────────────────────────────────────────────
def show_attract(screen, screen_size, attract, font_btn):
    """
    Draw the attract image plus a START button and a corner × close button.
    Returns (start_rect, close_rect).
    """
    sw, sh = screen_size
    screen.blit(attract, (0, 0))
    start_rect = draw_button(
        screen, "START", font_btn,
        (sw // 2, int(sh * 0.78)),
        BTN_BG_START,
    )
    close_rect = draw_close_button(screen, screen_size)
    pygame.display.flip()
    return start_rect, close_rect


def run_session(screen, screen_size, font_large, picam2, clock):
    """Run one full NUM_PHOTOS shoot, returning the list of file paths."""
    os.makedirs(TMP_DIR, exist_ok=True)
    paths = []
    for i in range(NUM_PHOTOS):
        path = os.path.join(TMP_DIR, f"photo_{i + 1}.jpg")
        shoot_photo(screen, screen_size, font_large,
                    path, i + 1, NUM_PHOTOS, picam2, clock)
        paths.append(path)
    return paths


# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    # Intentionally do NOT force SDL_VIDEODRIVER here. The old "fbcon" driver
    # is SDL 1.x only and is missing from the SDL 2 build that pygame 2 uses,
    # which makes pygame.init() silently fail and the next pygame call raise
    # "video system not initialized". Launched from the Pi desktop, SDL will
    # auto-pick X11 (or Wayland) and everything just works.
    pygame.display.init()
    pygame.font.init()

    info = pygame.display.Info()
    screen_size = (info.current_w, info.current_h)
    screen = pygame.display.set_mode(screen_size, pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)


    attract     = load_attract(screen_size)
    font_large  = pygame.font.SysFont(None, FONT_SIZE,    bold=True)
    font_btn    = pygame.font.SysFont(None, BTN_FONT_SIZE, bold=True)
    font_footer = pygame.font.SysFont(None, 48,           bold=True)
    clock       = pygame.time.Clock()

    picam2 = init_camera()
    if picam2 is None:
        print("[INFO] Live preview disabled "
              "(picamera2 not available or failed to start).")
    else:
        print("[INFO] Live camera preview active.")

    print("Photobooth ready — tap START on the screen.")

    try:
        while True:
            start_rect, close_rect = show_attract(
                screen, screen_size, attract, font_btn
            )
            idx = wait_for_click([start_rect, close_rect])
            if idx == -1 or idx == 1:   # ESC, window-close, or × tapped
                break

            # Inner loop so REDO retakes without going back to the attract screen
            while True:
                paths = run_session(screen, screen_size, font_large,
                                    picam2, clock)
                strip = compose_strip(paths, font_footer)
                action = show_review(screen, screen_size, strip, font_btn)

                if action == "save":
                    save_strip(strip)
                    break
                if action == "quit":
                    raise SystemExit
                # action == "redo": fall through and reshoot
                print("Redo requested — retaking session.")

    except (SystemExit, KeyboardInterrupt):
        print("Exiting.")
    finally:
        stop_camera(picam2)
        pygame.quit()


if __name__ == "__main__":
    main()
