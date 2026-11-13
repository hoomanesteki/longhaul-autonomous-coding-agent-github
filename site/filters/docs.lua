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

-- Links and images of an included doc are written relative to the doc's folder in the repo,
-- which is not where the page is. A div that carries `base` (the doc's folder, for example
-- base="docs") gets them pointed at the site: a link to a doc with a page goes to that page, an
-- image goes to its copy in _generated/, and a link to any other file of the repo goes to the
-- file on GitHub. scripts/site_pages.py writes the map from doc to page and the image copies.
local doc_pages = nil

local function load_doc_pages()
  if doc_pages then
    return doc_pages
  end
  local root = quarto.project.directory or "."
  local handle = io.open(root .. "/_generated/doc-pages.txt", "r")
  if not handle then
    error("cannot read _generated/doc-pages.txt. Run scripts/site_pages.py first, or use make site.")
  end
  doc_pages = {}
  for line in handle:lines() do
    local doc, page = line:match("^(%S+) (%S+)$")
    if doc then
      doc_pages[doc] = page
    end
  end
  handle:close()
  return doc_pages
end

-- "docs" and "../eval/chaos/README.md" give "eval/chaos/README.md". Nil when the path climbs
-- out of the repo.
local function join(base, path)
  local parts = {}
  for part in (base .. "/" .. path):gmatch("[^/]+") do
    if part == ".." then
      if #parts == 0 then
        return nil
      end
      table.remove(parts)
    elseif part ~= "." then
      table.insert(parts, part)
    end
  end
  return table.concat(parts, "/")
end

-- "../" once for each folder between the project and the page, so adr/ pages reach the root.
local function page_prefix()
  local input, root = quarto.doc.input_file, quarto.project.directory
  if not input or not root then
    return ""
  end
  local _, depth = pandoc.path.make_relative(input, root):gsub("/", "")
  return string.rep("../", depth)
end

-- Links to GitHub carry this prefix until Pandoc() below knows the repository from the
-- metadata, which is read after the divs.
local REPO = "repo:"

local function site_target(base, target, image)
  if target == "" or target:match("^#") or target:match("^/") or target:match("^%a[%w+.-]*:") then
    return nil
  end
  local path, anchor = target:match("^([^#]*)(.*)$")
  local repo_path = join(base, path)
  if not repo_path or repo_path == "" then
    return nil
  end
  if image then
    return page_prefix() .. "_generated/" .. repo_path
  end
  local page = load_doc_pages()[repo_path]
  if page then
    return page_prefix() .. page .. anchor
  end
  local folder = path:sub(-1) == "/"
  return REPO .. (folder and "tree" or "blob") .. "/main/" .. repo_path .. (folder and "/" or "") .. anchor
end

local function point_at_site(div, base)
  return div:walk({
    Link = function(link)
      local target = site_target(base, link.target, false)
      if target then
        link.target = target
        return link
      end
    end,
    Image = function(image)
      local target = site_target(base, image.src, true)
      if target then
        image.src = target
        return image
      end
    end,
  })
end

function Pandoc(doc)
  local repo = doc.meta["source-repo"] and pandoc.utils.stringify(doc.meta["source-repo"])
  return doc:walk({
    Link = function(link)
      if link.target:sub(1, #REPO) == REPO then
        if not repo then
          error("a link of an included doc needs source-repo in _quarto.yml")
        end
        link.target = repo .. "/" .. link.target:sub(#REPO + 1)
        return link
      end
    end,
  })
end

-- ::: {.drop-title} removes the included file's own H1, because the page already shows a title.
-- ::: {.demote} moves every heading down a level, so the H1 of each included file becomes a
-- section of a page that stacks several of them (the gate reports).
--
-- Both hand back the blocks without the div, and so does a div that only carries `base`.
-- Pandoc leaves headings that sit inside a div out of the table of contents, and the pages
-- need them there.
function Div(div)
  local base = div.attributes["base"]
  if base then
    div = point_at_site(div, base)
  end
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
  if base then
    return div.content
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
  ["Pending"] = "active",
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

-- A ```mermaid fence in an included file draws on GitHub, but Quarto only draws the cells it
-- writes itself (pre.mermaid-js, from a {mermaid} cell). Without this, the diagrams of
-- docs/architecture.md would show as code. The element and its class are the ones Quarto's
-- own runtime looks for, and the text goes in escaped because the runtime reads textContent.
function CodeBlock(block)
  if not block.classes:includes("mermaid") then
    return nil
  end
  local escaped = block.text:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;")
  -- The frame scrolls sideways, as a figure of a {mermaid} cell does (styles.css), because the
  -- diagram keeps a readable width that can be wider than the page.
  return pandoc.RawBlock(
    "html",
    '<div class="mermaid-frame"><pre class="mermaid mermaid-js">' .. escaped .. "</pre></div>"
  )
end
