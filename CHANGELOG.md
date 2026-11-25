# Changelog

## 1.0.10
### Improved
- **Smoother Arrange animation.** The glide used a separate 16 ms timer per card, which Windows rounds up to about 31 ms, so it ran at roughly 30 frames per second and took about 50% longer than intended. All cards now share one animator that follows the clock, asks Windows for a 1 ms timer while it runs, and moves every card in a single batched call per frame. Same look, about four times as many updates, and it always finishes on time. Background checks pause while cards glide.

## 1.0.9
### New
- **"Out of" bar in the weekly chart.** Behind each accent-colored bar there is now a grey bar showing that day's tasks (completed + deleted). Complete 6 and delete 4 and the grey bar shows 10 with the accent bar at 6, labelled "6/10". The header shows "6/10 today" in the same way. Deleting a task you had already completed does not count against you.
- **Clear progress.** A new "Clear progress..." button in Customize, with a confirmation, resets the weekly chart and empties the Done list. Your current tasks are kept.
- **Your font weight applies to the whole app.** The Font weight you pick in Customize (Regular / Semibold / Bold / Extra Bold) now styles every window, menu card, dialog and text box, not just the task cards. Headings are one step heavier. Long labels shrink slightly instead of overflowing.
### Changed
- **Lists that are not filled in go back to normal.** Choose Bullet or Numbered list and then click away or press Esc without adding any items, and the task simply stays a normal task.
- **Turning a list back into a normal task removes its items** and keeps only the headline.
- **Arrange cards** now keeps your highest card where it is and glides the rest into a neat column right underneath it, in their current top-to-bottom order. A full column continues to its right.

## 1.0.8
### New
- **Lists have a headline.** A bullet or numbered list now shows its first line as a larger headline, with a thin rule and then the items below it.
- **Type list items right on the card.** Choose Bullet list or Numbered list from the right-click menu: your task text becomes the headline and an empty box appears under it. Type an item and another empty box appears, and so on. Enter moves to the next line, Enter on an empty line (or the Save button, or clicking away) saves, Esc cancels, and Backspace on an empty line removes it. No more commas.
- **Edit by double-click.** Double-click a list card to edit its headline and items; double-click a normal card to rename it.
- Existing lists from 1.0.7 are converted automatically (the first line becomes the headline).
### Fixed
- **Flickering.** When two cards overlapped, the top one flickered now and then, and sometimes every card did. A background check re-sent every card to its layer several times a second, which briefly swapped overlapping cards. DeskTask now changes the stacking only when something actually changes (focus, the desktop being shown, a card being dragged or added) and does nothing while idle or while you drag.
- Cards no longer fight Windows over their position if the system keeps its own.

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