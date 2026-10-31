-- Pandoc filter for the Longhaul docs site. site/_quarto.yml lists it under `filters`.
--
-- The pages pull Markdown from docs/ with the include shortcode. Those files were written
-- to be read on GitHub, so a few things need adjusting once they sit inside a site page.

local SITE_NAME = "Longhaul"

-- Tab title. Quarto's template joins the page title and the site name with an en dash, which
-- the voice guide bans. _quarto.yml blanks the site name prefix, and this sets the whole title.
function Meta(meta)
  if meta.title then
    local title = pandoc.utils.stringify(meta.title)
    if title ~= SITE_NAME then
      title = title .. " | " .. SITE_NAME
    end
    meta.pagetitle = pandoc.MetaString(title)
  end
  return meta
end

local function first_h1(blocks)
  for index, block in ipairs(blocks) do
    if block.t == "Header" and block.level == 1 then
      return index
    end
  end
  return nil
end

-- ::: {.drop-title} removes the included file's own H1, because the page already shows a title.
-- ::: {.demote} moves every heading down a level, so the H1 of each included file becomes a
-- section of a page that stacks several of them (the gate reports).
--
-- Both hand back the blocks without the div. Pandoc leaves headings that sit inside a div out
-- of the table of contents, and the pages need them there.
function Div(div)
  if div.classes:includes("drop-title") then
    local index = first_h1(div.content)
    if index then
      div.content:remove(index)
    end
    return div.content
  end
  if div.classes:includes("demote") then
    return div:walk({
      Header = function(header)
        header.level = math.min(header.level + 1, 6)
        return header
      end,
    }).content
  end
end

-- Three changes to every table. A cell that holds only a status word becomes a badge (the
-- words are the ones in the progress table, the ADR index and the gate reports). The table sits
-- in a box that scrolls sideways, because the docs hold wide ones. And the columns get widths
-- that follow their content (see fit_columns). Badges and the box are styled in styles.css.
local BADGES = {
  ["Done"] = "done",
  ["Accepted"] = "done",
  ["Pass"] = "done",
  ["In progress"] = "active",
  ["Proposed"] = "active",
  ["Pending owner"] = "active",
  ["Blocked"] = "blocked",
  ["Fail"] = "blocked",
  ["Todo"] = "idle",
  ["Not started"] = "idle",
  ["Planned"] = "idle",
}

-- Pandoc gives the columns of a wide pipe table equal widths, because the separator row has
-- the same number of dashes everywhere. That makes an ID column as wide as a description. A
-- table whose widths are all equal is resized by the longest cell in each column, within limits
-- so one long column cannot squeeze the others away, and with room for the cell padding. A
-- table with default widths, or with widths its author chose, is left alone.
local function fit_columns(tbl)
  local specs = tbl.colspecs
  local first = specs[1] and specs[1][2]
  if not first then
    return
  end
  for _, spec in ipairs(specs) do
    if spec[2] ~= first then
      return
    end
  end
  local longest = {}
  for index = 1, #specs do
    longest[index] = 0
  end
  local function measure(rows)
    for _, row in ipairs(rows) do
      for index, cell in ipairs(row.cells) do
        local length = utf8.len(pandoc.utils.stringify(cell.contents)) or 0
        longest[index] = math.max(longest[index] or 0, length)
      end
    end
  end
  measure(tbl.head.rows)
  for _, body in ipairs(tbl.bodies) do
    measure(body.body)
  end
  local total = 0
  for index, length in ipairs(longest) do
    longest[index] = math.min(math.max(length + 4, 8), 52)
    total = total + longest[index]
  end
  for index, spec in ipairs(specs) do
    specs[index] = { spec[1], longest[index] / total }
  end
  tbl.colspecs = specs
end

function Table(tbl)
  fit_columns(tbl)
  local marked = tbl:walk({
    Plain = function(plain)
      local kind = BADGES[pandoc.utils.stringify(plain)]
      if kind then
        local badge = pandoc.Span(plain.content, pandoc.Attr("", { "badge-status", kind }))
        return pandoc.Plain({ badge })
      end
    end,
  })
  return pandoc.Div({ marked }, pandoc.Attr("", { "table-scroll" }))
end
