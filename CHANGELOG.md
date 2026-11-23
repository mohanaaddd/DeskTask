# Changelog

## 1.0.7
### New
- **Right-click menu.** Right-clicking a card opens a small rounded container beside it with three equal buttons: **Bullet list**, **Numbered list** and **Delete**. Hovering a button inverts its colors and shows an info card explaining it. Clicking the active list type turns the task back into a normal one.
- **List tasks.** Bulleted and numbered cards with neatly wrapped, indented items. Separate items with commas; if a new list has only one item, the add bar opens so you can add more.
### Fixed
- Cards could swap places on their own, so a card dragged over another one could vanish under it until you moved away. Cards now keep a stable front-to-back order, the card you touched last stays in front, and the layer watchdog does nothing while you are dragging.
- Delete still asks for confirmation, and the confirmation (and the Tasks list) now shows list tasks as a single readable line.

## 1.0.6
- Fixed: cards briefly showing above windows when switching from the desktop to a window.
- Cards now react to every focus change within about 30 ms, and are only lifted when the desktop itself covers them.

## 1.0.5
- Cards sit on the lowest layer of the screen by default and stay visible when the desktop is shown.
- New **Keep cards above windows** switch (highest layer).
- **Arrange cards** now glides the cards into place, one after another.

## 1.0.4
- Fixed: cards disappeared when showing the desktop (Win+D or the corner button).

## 1.0.3
- Fixed: a new card could land exactly on top of an existing one, so the old task looked like it disappeared. New cards now go to the first free spot, and cards that were already stacked are separated on startup.
- New **Capitalize Each Word** option in Customize (display only; your text is never changed).
- New **Arrange cards** item in the tray menu.

## 1.0.0
- Desktop task cards with drag-to-complete and an animated green check button.
- Delete with confirmation; deleted tasks can be restored.
- Tasks window: current, done and deleted tabs, plus a weekly (Saturday to Friday) progress chart.
- Customize: light/dark theme, accent colors, text size, opacity, four font weights.
- System tray app: double-click for tasks, Start with Windows, Ctrl+Alt+N to add a task.
- First-run visual tutorial with starter tasks.
- Automatic saving, a backup copy, and progress history.