import os
import time
import datetime
import subprocess
import pygame

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

# Under X11/Wayland on Bookworm, the libcamera preview is a regular OS window
# that will either cover or be covered by the pygame fullscreen window, which
# breaks the countdown overlay. We default the live preview off here and just
# show the countdown on a black screen, then flash + capture. Flip to True if
# you switch the capture pipeline to picamera2 (which can draw frames straight
# into a pygame surface).
SHOW_PREVIEW = False

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
def start_preview(screen_w, screen_h):
    """Launch libcamera-still as a full-screen preview."""
    if not SHOW_PREVIEW:
        return None
    preview_rect = f"0,0,{screen_w},{screen_h}"
    return subprocess.Popen([
        CAPTURE_CMD,
        "-p", preview_rect,
        "-t", "0",
    ])


def stop_preview(proc):
    """Terminate the preview subprocess cleanly (kill if it hangs)."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def capture_photo(path):
    """
    Snap a single JPEG to `path`. The screen should already be white when
    this is called so the subject is lit during the exposure.
    """
    subprocess.call([
        CAPTURE_CMD,
        "-t", str(CAPTURE_TIMEOUT),
        "--width", str(CAPTURE_WIDTH),
        "--height", str(CAPTURE_HEIGHT),
        "-n",
        "-o", path,
    ])


# ── Countdown + flash ──────────────────────────────────────────────────────────
def draw_countdown_number(screen, number, screen_size, font_large):
    """
    Paint the screen black (camera preview shows through) then draw:
      - a semi-transparent dark backing circle
      - a drop shadow
      - the countdown number
    """
    w, h = screen_size
    cx, cy = w // 2, h // 2

    screen.fill((0, 0, 0))

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


def flash_white(screen):
    """Fill the entire screen white — used as fill light during capture."""
    screen.fill((255, 255, 255))
    pygame.display.flip()


def shoot_photo(screen, screen_size, font_large, out_path, shot_num, total):
    """One full shoot cycle: preview + countdown + flash + capture."""
    preview = start_preview(*screen_size)
    time.sleep(PREVIEW_SETTLE)

    for n in range(COUNTDOWN_SECS, 0, -1):
        draw_countdown_number(screen, n, screen_size, font_large)
        time.sleep(COUNTDOWN_HOLD)

    flash_white(screen)
    time.sleep(FLASH_SETTLE)
    stop_preview(preview)
    capture_photo(out_path)

    # Keep flashing white briefly so the transition looks intentional, then
    # clear to black so the next preview launch isn't masked by white.
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
    pygame.display.flip()

    idx = wait_for_click([save_rect, redo_rect])
    if idx == 0:
        return "save"
    if idx == 1:
        return "redo"
    return "quit"


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
    """Draw the attract image plus a START button; return the button rect."""
    sw, sh = screen_size
    screen.blit(attract, (0, 0))
    start_rect = draw_button(
        screen, "START", font_btn,
        (sw // 2, int(sh * 0.78)),
        BTN_BG_START,
    )
    pygame.display.flip()
    return start_rect


def run_session(screen, screen_size, font_large):
    """Run one full NUM_PHOTOS shoot, returning the list of file paths."""
    os.makedirs(TMP_DIR, exist_ok=True)
    paths = []
    for i in range(NUM_PHOTOS):
        path = os.path.join(TMP_DIR, f"photo_{i + 1}.jpg")
        shoot_photo(screen, screen_size, font_large,
                    path, i + 1, NUM_PHOTOS)
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

    print("Photobooth ready — tap START on the screen.")

    try:
        while True:
            start_rect = show_attract(screen, screen_size, attract, font_btn)
            if wait_for_click(start_rect) == -1:
                break

            # Inner loop so REDO retakes without going back to the attract screen
            while True:
                paths = run_session(screen, screen_size, font_large)
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
        pygame.quit()


if __name__ == "__main__":
    main()
