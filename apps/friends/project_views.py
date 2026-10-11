"""App-JWT-only project API; runtime credentials never authorize a mutation."""

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from apps.tenants.authentication import JWTAuthenticationWithRLS

from . import project_services as projects
from .project_flags import projects_v2_enabled
from .views import FriendsView


class ProjectView(FriendsView):
    authentication_classes = [JWTAuthenticationWithRLS]

    def get_tenant(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not projects_v2_enabled(tenant):
            raise NotFound("No such endpoint.")
        return super().get_tenant(request)

    def data(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError("An object is required.")
        return request.data

    def handle_exception(self, exc):
        """Project refusals are sentences written for the person ("You already asked
        about this step today."). DRF renders a bare-string ValidationError as a JSON
        list, which the apps don't show; send it as ``non_field_errors`` so they do."""
        if isinstance(exc, ValidationError) and isinstance(exc.detail, list):
            exc = ValidationError({"non_field_errors": exc.detail})
        return super().handle_exception(exc)


class PlanView(ProjectView):
    def get(self, request, mission_id):
        return Response(projects.get_plan(self.get_tenant(request), mission_id))


class StepsView(ProjectView):
    def post(self, request, mission_id):
        step = projects.create_step(self.get_tenant(request), request.user, mission_id, self.data(request))
        return Response({"step_id": str(step.id), "version": step.version}, status=201)


class StepView(ProjectView):
    def patch(self, request, mission_id, step_id):
        step = projects.patch_step(self.get_tenant(request), mission_id, step_id, self.data(request))
        return Response({"step_id": str(step.id), "version": step.version})

    def delete(self, request, mission_id, step_id):
        projects.delete_step(self.get_tenant(request), mission_id, step_id)
        return Response(status=204)


class MembersView(ProjectView):
    """POST more of my neighbors into a project I started (they get an invitation)."""

    def post(self, request, mission_id):
        invited = projects.add_members(
            self.get_tenant(request), mission_id, self.data(request).get("member_friendship_ids")
        )
        return Response({"invited": len(invited)}, status=201)


class ProjectDeleteView(ProjectView):
    """POST: an owner deletes the project for everyone."""

    def post(self, request, mission_id):
        projects.delete_project(self.get_tenant(request), mission_id)
        return Response({"mission_id": str(mission_id), "status": "abandoned"})


class StepBackView(ProjectView):
    """POST: let go of some or all of my open steps, staying in the project."""

    def post(self, request, mission_id):
        released = projects.step_back(self.get_tenant(request), request.user, mission_id, self.data(request))
        return Response({"released": len(released)})


class OwnersView(ProjectView):
    """POST: an owner makes another member an owner too, or steps down."""

    def post(self, request, mission_id):
        target = projects.set_owner_role(self.get_tenant(request), request.user, mission_id, self.data(request))
        return Response({"membership_id": str(target.id), "role": target.role})


class LinkedProjectsView(ProjectView):
    """GET my projects linked to my own Horizons goals."""

    def get(self, request):
        return Response(projects.linked_projects(self.get_tenant(request)))


class StepActionView(ProjectView):
    def post(self, request, mission_id, step_id, action):
        tenant = self.get_tenant(request)
        data = self.data(request)
        if action == "ask":
            projects.ask(tenant, request.user, mission_id, step_id, data.get("membership_ids"))
            return Response({"step_id": str(step_id), "status": "asked"})
        if action == "respond":
            assignment = projects.respond(tenant, request.user, mission_id, step_id, data)
            return Response({"assignment_id": str(assignment.id), "status": assignment.status})
        if action == "second-look":
            step = projects.set_second_look(tenant, mission_id, step_id, data.get("on"))
        elif action == "confirm":
            step = projects.confirm(tenant, request.user, mission_id, step_id)
        elif action == "question":
            step = projects.question(tenant, request.user, mission_id, step_id, data)
        elif action == "keep":
            step = projects.keep_step(tenant, mission_id, step_id)
        else:
            step = projects.complete(tenant, request.user, mission_id, step_id, reopen=action == "reopen", data=data)
        return Response({"step_id": str(step.id), "status": step.status, "version": step.version})


class MilestonesView(ProjectView):
    def post(self, request, mission_id):
        item = projects.milestone_write(self.get_tenant(request), mission_id, self.data(request))
        return Response({"milestone_id": str(item.id)}, status=201)


class MilestoneView(ProjectView):
    def patch(self, request, mission_id, milestone_id):
        item = projects.milestone_write(
            self.get_tenant(request), mission_id, self.data(request), milestone_id=milestone_id
        )
        return Response({"milestone_id": str(item.id)})

    def delete(self, request, mission_id, milestone_id):
        projects.milestone_write(self.get_tenant(request), mission_id, milestone_id=milestone_id, delete=True)
        return Response(status=204)


class DependenciesView(ProjectView):
    def post(self, request, mission_id):
        item = projects.dependency_write(self.get_tenant(request), mission_id, self.data(request))
        return Response({"dependency_id": str(item.id)}, status=201)


class DependencyView(ProjectView):
    def delete(self, request, mission_id, dependency_id):
        projects.dependency_write(self.get_tenant(request), mission_id, dependency_id=dependency_id)
        return Response(status=204)


class MembershipView(ProjectView):
    """PATCH my own membership: ``linked_goal_id`` (my Horizons goal, or null) and/or
    ``muted`` (no pushes from this project for me; nobody else sees it)."""

    def patch(self, request, mission_id):
        data = self.data(request)
        if set(data) - {"linked_goal_id", "muted"} or not data:
            raise ValidationError("Only linked_goal_id and muted can be changed here.")
        tenant = self.get_tenant(request)
        if "muted" in data and type(data["muted"]) is not bool:
            raise ValidationError("muted must be true or false.")
        member = None
        if "linked_goal_id" in data:
            member = projects.set_linked_goal(tenant, mission_id, data.get("linked_goal_id"))
        if "muted" in data:
            member = projects.set_muted(tenant, mission_id, data["muted"])
        return Response(
            {"linked_goal_id": str(member.linked_goal_id) if member.linked_goal_id else None, "muted": member.muted}
        )


# ── Assistant drafts + proposals: the HUMAN side (app JWT only) ────────────────


class DraftsView(ProjectView):
    def get(self, request):
        from . import project_assistant as assistant

        return Response(assistant.list_drafts(self.get_tenant(request)))


class DraftView(ProjectView):
    def get(self, request, draft_id):
        from . import project_assistant as assistant

        return Response(assistant.get_draft(self.get_tenant(request), draft_id))

    def patch(self, request, draft_id):
        from . import project_assistant as assistant

        return Response(assistant.update_draft(self.get_tenant(request), draft_id, self.data(request).get("payload")))

    def delete(self, request, draft_id):
        from . import project_assistant as assistant

        assistant.delete_draft(self.get_tenant(request), draft_id)
        return Response(status=204)


class DraftPublishView(ProjectView):
    def post(self, request, draft_id):
        from . import project_assistant as assistant

        extra = self.data(request).get("member_friendship_ids") or []
        if not isinstance(extra, list):
            raise ValidationError("member_friendship_ids must be a list.")
        result = assistant.publish_draft(self.get_tenant(request), request.user, draft_id, extra)
        return Response(result, status=201)


class ProposalsView(ProjectView):
    """GET my pending suggestions (optionally ``?mission_id=``)."""

    def get(self, request):
        from . import project_assistant as assistant

        return Response(assistant.list_proposals(self.get_tenant(request), request.query_params.get("mission_id")))


class ProposalActionView(ProjectView):
    def post(self, request, proposal_id, action):
        from . import project_assistant as assistant

        tenant = self.get_tenant(request)
        if action == "approve":
            return Response(assistant.approve(tenant, request.user, proposal_id))
        return Response(assistant.reject(tenant, proposal_id))
