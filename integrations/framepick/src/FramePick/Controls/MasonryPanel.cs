using System.Windows;
using System.Windows.Controls;

namespace FramePick.Controls;

public sealed class MasonryPanel : Panel
{
    public static readonly DependencyProperty MinColumnWidthProperty = DependencyProperty.Register(
        nameof(MinColumnWidth), typeof(double), typeof(MasonryPanel),
        new FrameworkPropertyMetadata(250d, FrameworkPropertyMetadataOptions.AffectsMeasure));

    public static readonly DependencyProperty SpacingProperty = DependencyProperty.Register(
        nameof(Spacing), typeof(double), typeof(MasonryPanel),
        new FrameworkPropertyMetadata(16d, FrameworkPropertyMetadataOptions.AffectsMeasure));

    private readonly List<Rect> _layouts = new();
    private int _columns = 1;
    private double _columnWidth;

    public double MinColumnWidth
    {
        get => (double)GetValue(MinColumnWidthProperty);
        set => SetValue(MinColumnWidthProperty, value);
    }

    public double Spacing
    {
        get => (double)GetValue(SpacingProperty);
        set => SetValue(SpacingProperty, value);
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        var width = double.IsInfinity(availableSize.Width) ? MinColumnWidth : Math.Max(MinColumnWidth, availableSize.Width);
        _columns = Math.Max(1, Math.Min(5, (int)((width + Spacing) / (MinColumnWidth + Spacing))));
        _columnWidth = (width - Spacing * (_columns - 1)) / _columns;
        var heights = new double[_columns];
        _layouts.Clear();

        foreach (UIElement child in InternalChildren)
        {
            child.Measure(new Size(_columnWidth, double.PositiveInfinity));
            var column = Array.IndexOf(heights, heights.Min());
            var y = heights[column];
            _layouts.Add(new Rect(column * (_columnWidth + Spacing), y, _columnWidth, child.DesiredSize.Height));
            heights[column] += child.DesiredSize.Height + Spacing;
        }

        var desiredHeight = heights.Length == 0 ? 0 : Math.Max(0, heights.Max() - Spacing);
        return new Size(width, desiredHeight);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        for (var i = 0; i < InternalChildren.Count && i < _layouts.Count; i++)
            InternalChildren[i].Arrange(_layouts[i]);
        return finalSize;
    }
}
