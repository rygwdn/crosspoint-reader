#pragma once

#include <SdCardFontRegistry.h>

#include <cstdint>
#include <string>
#include <vector>

#include "TextSettingsPreview.h"
#include "activities/UiTabListActivity.h"
#include "components/OptionPopup.h"
#include "components/themes/BaseTheme.h"

// Reader text settings with a shared live preview pane: tab bar
// (Font | Size | Layout | Style) is position 0 of the Up/Down nav ring, same
// idiom as SettingsActivity. Family/Size rows apply on Confirm; Layout/Style
// rows toggle or open an OptionPopup picker. (Tab::Family/Style are the enum
// names for the Font/Style tabs.)
class TextSettingsActivity final : public UiTabListActivity {
 public:
  enum class Tab : uint8_t { Family, Size, Layout, Style, Count };

  TextSettingsActivity(GfxRenderer& renderer, MappedInputManager& mappedInput, const SdCardFontRegistry* registry,
                       Tab initialTab = Tab::Family);

  void onEnter() override;
  void render(RenderLock&&) override;

 private:
  // Row indices per tab. enum class (not plain enum) so a LayoutRow can't be
  // silently confused with a StyleRow of equal value.
  enum class LayoutRow { LineSpacing, ParaSpacing, Alignment, ScreenMargin, Count };
  enum class StyleRow { FocusReading, Hyphenation, EmbeddedStyle, AntiAliasing, Count };

  // --- UiTabListActivity contract ---
  int listCount() const override;
  int tabCount() const override { return static_cast<int>(Tab::Count); }
  int activeTab() const override { return static_cast<int>(tab_); }
  const char* tabLabel(int index) const override;
  void buildScreen(UiScreen& screen) override;
  void activateIndex(int index) override;
  void onTabAction(int index) override;
  void stepTab(int direction) override { switchTab(direction); }
  bool handleButtons() override;
  bool handleCustomInput() override;

  void applyFamily(int listIndex);
  void applySize(int listIndex);
  // Repopulates sizes_ (and currentSizeIndex_) from the active family's
  // installed point sizes. Call after any family change.
  void rebuildSizeList();
  void confirmLayoutRow(int row);
  void confirmStyleRow(int row);
  // Applies the row at the given list index for the active tab (Confirm and tap share this).
  void activateRow(int row);

  std::string layoutValueText(int row) const;
  std::string styleValueText(int row) const;
  // Button-hint label for Confirm at the current ring position.
  const char* confirmLabelText() const;
  // True when the focused list row is a setting the preview cannot reflect.
  bool focusedRowHasNoPreview() const;
  // True when the active tab's list (Family or Size) has at least one row
  // marked with the flash-cache "*" -- gates the legend caption below the list.
  bool listHasFlashCacheMarker() const;
  void switchTab(int direction = 1);

  // Row storage for the active tab: rowItems_ (label/actionValue) is
  // rebuilt only when the tab or its backing data changes (rebuildRowItems(),
  // called from onEnter()/onTabAction()/switchTab()); rowValues_ holds the
  // live per-row value text, refreshed every buildScreen() call by assigning
  // into the existing strings (no vector growth), so steady-state rendering
  // never allocates/frees row storage.
  std::vector<std::string> rowValues_;
  // Backing storage for rowItems_[i].label -- owns the text (incl. any
  // flash-cache "*" suffix) since ListItem only holds a raw pointer.
  std::vector<std::string> rowLabels_;
  std::vector<freeink::ui::ListItem> rowItems_;
  void rebuildRowItems();

  struct FontEntry {
    std::string name;
    bool isBuiltin;
    uint8_t settingIndex;
    // False if the file that would actually load for this family (at the
    // current point size, via findNearestSize) is too large for the SD-font
    // flash-cache partition -- see wouldFitFlashCache(). Always true for
    // builtin fonts (compiled into flash already, no SD file involved).
    bool flashFits = true;
  };

  struct SizeEntry {
    std::string name;  // the point size, rendered for display ("14 pt")
    uint8_t pointSize;
    // False if this exact size's file is too large for the flash-cache
    // partition. Always true for builtin-font point sizes.
    bool flashFits = true;
  };

  // Best-effort prediction of whether `file` would successfully promote to
  // flash residency, without touching the flash-cache partition itself:
  // stats the .cpfont's size on SD and compares it to
  // SdFontFlashCache::payloadCapacity(). Defaults to true (no "*" marker)
  // whenever the answer can't be determined (null file, unreadable partition,
  // unstatable file) -- the indicator should only ever claim a size won't
  // fit when there's positive evidence of that, never guess.
  bool wouldFitFlashCache(const SdCardFontFileInfo* file) const;

  const SdCardFontRegistry* registry_;
  OptionPopup optionPopup_;
  std::vector<FontEntry> fonts_;
  std::vector<SizeEntry> sizes_;
  textsettings::PreviewLayout previewLayout_;  // cached preview line layout; relaid only on setting/geometry change

  Tab tab_;
  int currentFamilyIndex_ = 0;
  int currentSizeIndex_ = 0;

  ThemeMetrics metrics_ = {};
  int afterHeader = 0;
  int bottomReserved = 0;
  int usableHeight = 0;
  int previewHeight = 0;
};
