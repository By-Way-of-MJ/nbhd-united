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
        step = projects.complete(tenant, request.user, mission_id, step_id, reopen=action == "reopen")
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
    """PATCH my own membership: ``linked_goal_id`` (my Horizons goal, or null)."""

    def patch(self, request, mission_id):
        data = self.data(request)
        if set(data) - {"linked_goal_id"} or "linked_goal_id" not in data:
            raise ValidationError("Only linked_goal_id can be changed here.")
        member = projects.set_linked_goal(self.get_tenant(request), mission_id, data.get("linked_goal_id"))
        return Response({"linked_goal_id": str(member.linked_goal_id) if member.linked_goal_id else None})
