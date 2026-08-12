---
title: Projects
nav:
  order: 2
  tooltip: Current and past lab projects
---

# <i class="fas fa-project-diagram"></i> Projects

Explore current and past projects from the Visual and Analytic Computing Lab.

Each project has its own page with a short overview and links to further information.

## Research areas

Our projects draw on expertise in medical image analysis, artificial
intelligence, data visualization, computer graphics, computer vision,
holographic and light-field displays, immersive video, and augmented, virtual,
and extended reality. We also study the perceptual and cognitive aspects of
advanced displays and visualization systems.

{% include section.html %}

{%- assign published_projects = site.projects | where_exp: "project", "project.published != false" -%}
{%- if published_projects.size > 0 -%}
  {% include list.html data="projects" component="project-card" %}
{%- else -%}
  <p class="center">Project information will be added soon.</p>
{%- endif -%}
